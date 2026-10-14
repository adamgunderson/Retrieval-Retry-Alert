"""FireMon REST client, standard library only.

Uses urllib rather than requests so the tool needs no install step on the FMOS
appliance -- pip itself is often unavailable there, and the third-party
site-packages path documented for it does not exist on every FMOS generation.
Mirrors the approach already proven in PolicyOptimizer-Email-Digest's
firemon_digest/api.py in this repo, for the same reason.
"""
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .model import DeviceHealth

logger = logging.getLogger(__name__)

PAGE_SIZE = 500


class ApiError(Exception):
    pass


class AuthenticationError(ApiError):
    pass


class UnsupportedDeviceError(ApiError):
    """Raised when FireMon reports that this specific device cannot be
    manually retrieved at all, regardless of its current health status --
    a permanent, device-type-level fact, not a transient failure to retry."""


def _build_ssl_context(verify_tls):
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _parse_json(raw, context):
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError as e:
        raise ApiError(f'{context}: response was not valid JSON ({e}): {raw[:200]!r}')


def _token_from_set_cookie(headers):
    """Pull the (already percent-encoded) authToken value out of Set-Cookie."""
    if headers is None:
        return None
    try:
        cookies = headers.get_all('Set-Cookie') or []
    except AttributeError:
        single = headers.get('Set-Cookie')
        cookies = [single] if single else []
    for cookie in cookies:
        for part in str(cookie).split(';'):
            name, sep, value = part.strip().partition('=')
            if sep and name.strip().lower() == 'authtoken' and value.strip():
                return value.strip()
    return None


class FireMonClient:
    def __init__(self, config):
        self._config = config
        self._ssl_context = _build_ssl_context(config.verify_tls)
        self._headers = {'Accept': 'application/json'}

    def _raw_request(self, method, path, body=None):
        url = f'{self._config.base_url}{path}'
        headers = dict(self._headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout,
                                         context=self._ssl_context) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
                return resp.status, raw, resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', errors='replace')
            return e.code, raw, e.headers
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            raise ApiError(f'{method} {path} failed: {e}')

    def authenticate(self):
        # Both calls take a JSON credentials body, not HTTP Basic Auth -- this
        # matches every other working FireMon script in this repo (e.g.
        # FireMon-PO-Bridge/firemon_auth.py), not the root CLAUDE.md's prose,
        # which is inaccurate on this point.
        creds = {'username': self._config.username, 'password': self._config.password}

        status, raw, _ = self._raw_request(
            'POST', '/securitymanager/api/authentication/validate', creds)
        if status not in (200, 204):
            raise AuthenticationError(f'Credential validation failed with status {status}: {raw[:200]}')

        status, raw, headers = self._raw_request(
            'POST', '/securitymanager/api/authentication/login', creds)
        if status != 200:
            raise AuthenticationError(f'Login failed with status {status}: {raw[:200]}')
        data = _parse_json(raw, 'Login')
        body_token = data.get('token') if isinstance(data, dict) else None
        cookie_token = _token_from_set_cookie(headers)
        if not body_token and not cookie_token:
            raise AuthenticationError('Login succeeded but no token was returned.')

        # Different FireMon builds have been observed (across scripts in this
        # repo) to accept the session token via different mechanisms -- an
        # X-FM-Auth-Token header, an Authorization: Bearer header, or an
        # authToken cookie. Send it every way at once rather than guessing;
        # the extras are ignored by a server that doesn't use them.
        self._headers['Content-Type'] = 'application/json'
        if body_token:
            self._headers['X-FM-Auth-Token'] = body_token
            self._headers['Authorization'] = f'Bearer {body_token}'
        cookie_value = cookie_token or (
            urllib.parse.quote(str(body_token), safe='') if body_token else None)
        if cookie_value:
            self._headers['Cookie'] = f'authToken={cookie_value}'
        logger.info('Authenticated with FireMon at %s', self._config.base_url)

    def _request(self, method, path, body=None, retry_auth=True):
        status, raw, _headers = self._raw_request(method, path, body)
        if status == 401 and retry_auth:
            logger.info('Session token rejected, re-authenticating...')
            self.authenticate()
            return self._request(method, path, body, retry_auth=False)
        return status, raw

    def get_device_health_list(self):
        domain_id = self._config.domain_id
        filter_param = ''
        if self._config.device_filter:
            filter_param = f'filter={urllib.parse.quote(self._config.device_filter)}&'
        devices = []
        page = 0
        total = None

        while total is None or len(devices) < total:
            path = (f'/securitymanager/api/domain/{domain_id}/device/health'
                    f'?{filter_param}page={page}&pageSize={PAGE_SIZE}')
            status, raw = self._request('GET', path)
            if status != 200:
                raise ApiError(f'Device health scan failed with status {status}: {raw[:300]}')
            data = _parse_json(raw, 'Device health scan')
            results = data.get('results', [])
            total = data.get('total', len(results))
            devices.extend(DeviceHealth.from_api(r) for r in results)
            if not results:
                break
            page += 1

        return devices

    def get_retrieval_targets(self):
        """{child_id: (target_id, target_name, target_device_type)} for every
        device whose retrieval target is ANOTHER device -- extendedSettingsJson
        .retrievalTargetDeviceId set and not its own id. Whether that means
        the child can be retrieved individually depends on what the parent
        is, which is why the parent's deviceType is returned and the
        exclusion decision is left to exclusions.is_excluded:

        * CLOUD_PROVIDER parent (Azure/AWS/GCP assets, VNETs, cloud elements):
          the child has no retrieval of its own -- seen live: hundreds of
          such children with an empty deviceStatuses list despite years of
          successful parent retrievals. A manual retrieval POSTed to one is
          accepted (204) but can only fail, and that failure is the
          RETRIEVAL_ERROR this tool would then alert on forever.
        * DEVICE_MGR parent (Firepower Management Center, Panorama, Meraki):
          the child IS retrieved individually, through the manager -- seen
          live: each FMC firewall carrying its own retrievalStatus entries,
          including "Successfully retrieved configuration (Manual)". Those
          stay in scope.

        Read from the paginated device list, because the health list carries
        neither extendedSettingsJson nor parents."""
        domain_id = self._config.domain_id
        targets = {}
        page = 0
        total = None
        seen = 0
        names = {}
        types = {}

        while total is None or seen < total:
            path = f'/securitymanager/api/domain/{domain_id}/device?page={page}&pageSize={PAGE_SIZE}'
            status, raw = self._request('GET', path)
            if status != 200:
                raise ApiError(f'Device list failed with status {status}: {raw[:300]}')
            data = _parse_json(raw, 'Device list')
            results = data.get('results', [])
            total = data.get('total', len(results))
            if not results:
                break
            seen += len(results)
            for entry in results:
                device_id = entry.get('id')
                if device_id is None:
                    continue
                names[device_id] = entry.get('name') or f'device-{device_id}'
                types[device_id] = ((entry.get('devicePack') or {}).get('deviceType') or '').upper()
                settings = entry.get('extendedSettingsJson') or {}
                target = settings.get('retrievalTargetDeviceId')
                if target is None:
                    continue
                try:
                    target = int(target)
                except (TypeError, ValueError):
                    continue
                if target == device_id:
                    continue
                parent_name = ''
                parent_type = ''
                for parent in entry.get('parents') or []:
                    if parent.get('id') == target:
                        parent_name = parent.get('name') or ''
                        parent_type = (parent.get('deviceType') or '').upper()
                        break
                targets[device_id] = (target, parent_name, parent_type)
            page += 1

        # A parent's name/type come from the parent's own list entry when the
        # child's parents[] did not carry them (or carried a different parent).
        return {child: (target,
                        name or names.get(target) or f'device-{target}',
                        ptype or types.get(target, ''))
                for child, (target, name, ptype) in targets.items()}

    def get_device_health(self, device_id):
        domain_id = self._config.domain_id
        path = f'/securitymanager/api/domain/{domain_id}/device/{device_id}/health'
        status, raw = self._request('GET', path)
        if status == 404:
            return None
        if status != 200:
            raise ApiError(f'Device {device_id} health check failed with status {status}')
        return DeviceHealth.from_api(_parse_json(raw, f'Device {device_id} health check'))

    def trigger_manual_retrieval(self, device_id):
        domain_id = self._config.domain_id
        path = f'/securitymanager/api/domain/{domain_id}/device/{device_id}/manualretrieval'
        status, raw = self._request('POST', path)
        if status not in (200, 202, 204):
            # A specific, permanent, device-type-level fact ("this device can
            # never be manually retrieved"), not a transient failure -- e.g.
            # {"success":false,"message":"Device : Device with name X does
            # not support manual retrieval"}. Both the 400 and the phrase are
            # required so an unrelated 400 can't be misread as this.
            if status == 400 and 'does not support manual retrieval' in raw.lower():
                raise UnsupportedDeviceError(
                    f'Device {device_id} does not support manual retrieval: {raw[:300]}')
            raise ApiError(f'Manual retrieval trigger failed with status {status}: {raw[:300]}')
