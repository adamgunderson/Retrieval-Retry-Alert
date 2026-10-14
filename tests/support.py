from datetime import datetime, timezone

from retry_alert.api import ApiError, UnsupportedDeviceError
from retry_alert.config import DigestConfig, FiremonConfig, RetryConfig, SmtpConfig
from retry_alert.model import DeviceHealth


def make_device(device_id, name, status='SUCCESS', timestamp=None, status_description='',
                 description='', licenses=('SM',)):
    ts = timestamp or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return DeviceHealth(id=device_id, name=name, status=status, status_description=status_description,
                         timestamp=ts, description=description, licenses=tuple(licenses), raw={})


def make_retry_config(state_dir, **overrides):
    defaults = dict(
        failure_statuses={'RETRIEVAL_ERROR', 'NORMALIZATION_ERROR', 'FAILURE'},
        stale_after_hours=24,
        grace_period_minutes=15,
        state_dir=state_dir,
        ignore_orphaned=True,
        ignore_unlicensed=True,
        exclude_name_contains=(),
        ignore_child_devices=True,
    )
    defaults.update(overrides)
    return RetryConfig(**defaults)


def make_digest_config(**overrides):
    defaults = dict(interval_hours=24)
    defaults.update(overrides)
    return DigestConfig(**defaults)


def make_firemon_config(**overrides):
    defaults = dict(
        base_url='https://example.invalid', domain_id='1',
        device_filter="DEVICEPACK.DEVICETYPE = 'MANAGEMENT_STATION'",
        username='svc', password='secret', verify_tls=False, timeout=30,
    )
    defaults.update(overrides)
    return FiremonConfig(**defaults)


def make_smtp_config(**overrides):
    defaults = dict(
        host='localhost', port=25, use_starttls=False, username='', password='',
        from_address='alerts@example.invalid', from_name='Retry Alert',
        recipients=['ops@example.invalid'], reply_to='', subject_prefix='', timeout=30,
    )
    defaults.update(overrides)
    return SmtpConfig(**defaults)


class FakeClient:
    """Duck-types FireMonClient for engine tests -- no network involved."""

    def __init__(self, devices=None):
        self.devices = {d.id: d for d in (devices or [])}
        self.triggered = []
        self.trigger_should_fail = set()
        self.trigger_unsupported = set()
        self.health_lookup_calls = []
        self.list_should_fail = False
        # {child_id: (parent_id, parent_name)}, as get_retrieval_targets()
        # returns it. Empty = every device retrieves on its own.
        self.retrieval_targets = {}
        self.targets_should_fail = False

    def set_device(self, device):
        self.devices[device.id] = device

    def get_retrieval_targets(self):
        if self.targets_should_fail:
            raise ApiError('simulated device list failure')
        return dict(self.retrieval_targets)

    def get_device_health_list(self):
        if self.list_should_fail:
            raise ApiError('simulated health scan failure')
        return list(self.devices.values())

    def get_device_health(self, device_id):
        self.health_lookup_calls.append(device_id)
        return self.devices.get(device_id)

    def trigger_manual_retrieval(self, device_id):
        self.triggered.append(device_id)
        if device_id in self.trigger_unsupported:
            raise UnsupportedDeviceError(f'Device {device_id} does not support manual retrieval: simulated')
        if device_id in self.trigger_should_fail:
            raise ApiError('simulated trigger failure')
