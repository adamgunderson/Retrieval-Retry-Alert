import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import ApiError, UnsupportedDeviceError
from .exclusions import is_excluded
from .model import (
    ALERT_RETRY_FAILED, ALERT_RETRY_TIMEOUT, ALERT_TRIGGER_ERROR, ALERT_UNSUPPORTED_DEVICE,
    REASON_FAILURE_STATUS, REASON_STALE,
    AlertRecord, PendingRetry,
)

try:
    import fcntl
    HAVE_FCNTL = True
except ImportError:  # Windows dev machines; production target is FMOS (Linux).
    HAVE_FCNTL = False

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_ALERTED = 2


class LockHeld(Exception):
    pass


class StateStore:
    """Small JSON state files under retry.state_dir, written atomically."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_fh = None

    def acquire_lock(self):
        if not HAVE_FCNTL:
            logger.debug('fcntl unavailable on this platform -- skipping overlap lock.')
            return
        lock_path = self.state_dir / '.lock'
        fh = open(lock_path, 'w')
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockHeld(f'Another run already holds {lock_path} -- skipping this cycle.')
        self._lock_fh = fh

    def release_lock(self):
        if self._lock_fh is None:
            return
        if HAVE_FCNTL:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
        self._lock_fh.close()
        self._lock_fh = None

    def _read(self, name):
        path = self.state_dir / name
        if not path.exists():
            return {}
        try:
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning('Could not read state file %s (%s) -- starting fresh.', path, e)
            return {}

    def _write(self, name, data):
        path = self.state_dir / name
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_dir), prefix=f'.{name}.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_pending(self):
        result = {}
        for k, v in self._read('pending.json').items():
            try:
                result[int(k)] = PendingRetry.from_json(v)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning('Dropping malformed pending-retry entry for device %s: %s', k, e)
        return result

    def save_pending(self, pending):
        self._write('pending.json', {str(k): v.to_json() for k, v in pending.items()})

    def append_ledger(self, alerts):
        """Appends alert-worthy events to the digest ledger. Never rewrites the
        whole file -- a crash mid-append loses at most the entries not yet
        flushed, not the ones already recorded."""
        if not alerts:
            return
        path = self.state_dir / 'ledger.jsonl'
        with open(path, 'a', encoding='utf-8') as fh:
            for alert in alerts:
                fh.write(json.dumps(alert.to_json(), sort_keys=True))
                fh.write('\n')

    def read_ledger(self):
        path = self.state_dir / 'ledger.jsonl'
        if not path.exists():
            return []
        entries = []
        with open(path, encoding='utf-8') as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(AlertRecord.from_json(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                    logger.warning('Skipping malformed ledger line %d: %s', line_no, e)
        return entries

    def prune_ledger(self, before):
        """Rewrites the ledger keeping only entries strictly after `before`.
        Called after a digest send with before=now: every entry just reported
        shares that exact `now` as its own timestamp (AlertRecord.timestamp is
        stamped at detection time, not from device data), so a >= comparison
        would never drop them -- they'd sit in the ledger and get re-reported
        by the very next digest check forever. A strict > correctly empties
        the ledger of everything up to and including this instant."""
        entries = [e for e in self.read_ledger() if e.timestamp > before]
        path = self.state_dir / 'ledger.jsonl'
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_dir), prefix='.ledger.jsonl.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                for entry in entries:
                    fh.write(json.dumps(entry.to_json(), sort_keys=True))
                    fh.write('\n')
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_digest_state(self):
        return self._read('digest_state.json')

    def save_digest_state(self, digest_state):
        self._write('digest_state.json', digest_state)

    def clear_ledger(self):
        try:
            (self.state_dir / 'ledger.jsonl').unlink()
        except FileNotFoundError:
            pass

    def load_unsupported(self):
        """Devices FireMon has told us, via a real API response, can never be
        manually retrieved -- learned at runtime, not derivable from a
        device's own attributes the way orphaned/unlicensed are."""
        return self._read('unsupported_devices.json')

    def save_unsupported(self, unsupported):
        self._write('unsupported_devices.json', unsupported)


@dataclass
class CycleResult:
    resolved: list = field(default_factory=list)
    # Alert-worthy events recorded to the digest ledger this cycle -- not
    # emailed immediately; a digest batches these up, see digest.py.
    recorded: list = field(default_factory=list)
    triggered: list = field(default_factory=list)
    exit_code: int = EXIT_OK


class RetryAlertEngine:
    def __init__(self, client, config, state_store, now=None, dry_run=False):
        self.client = client
        self.config = config
        self.state = state_store
        self.now = now or datetime.now(timezone.utc)
        self.dry_run = dry_run

    def _is_failure(self, device):
        return device.status in self.config.retry.failure_statuses

    def _is_stale(self, device):
        return self.now - device.timestamp > timedelta(hours=self.config.retry.stale_after_hours)

    def run_cycle(self):
        pending = self.state.load_pending()
        unsupported = self.state.load_unsupported()
        unsupported_ids = {int(k) for k in unsupported}

        scan = self.client.get_device_health_list()
        kept, excluded_by_reason, excluded_by_id = [], {}, {}
        for device in scan:
            excluded, reason = is_excluded(device, self.config, unsupported_ids)
            if excluded:
                excluded_by_reason.setdefault(reason, []).append(device.name)
                excluded_by_id[device.id] = reason
            else:
                kept.append(device)
        for reason, names in excluded_by_reason.items():
            logger.info('Ignoring %d device(s) excluded (%s): %s', len(names), reason, ', '.join(names))
        scan = kept
        scan_by_id = {d.id: d for d in scan}

        resolved = []
        alerts = []
        # A device that alerts here (still failing / no confirmation) is cleared
        # from `pending` below, but the trigger loop right after this one will
        # immediately queue it again this same cycle if it's still unhealthy --
        # retries keep going every cycle regardless of whether a digest is due.
        # This carries its attempt count forward onto the new PendingRetry, so
        # --list-pending reflects how many consecutive attempts a device has
        # actually had, not just "1" forever.
        carried_attempts = {}

        for device_id, retry in list(pending.items()):
            # A device that became excluded AFTER its retry was triggered
            # (an operator added its name to exclude_name_contains, or it was
            # learned to be unsupported) must not be re-checked and alerted on
            # as "still failing" -- the operator just said to stop watching
            # it. Drop the pending entry quietly instead. Checked against the
            # scan's own exclusion verdict first so no extra API call is spent
            # re-fetching a device we already decided to ignore.
            if device_id in excluded_by_id:
                logger.info('Device %s (%s) is now excluded (%s) -- clearing pending retry without alerting.',
                            device_id, retry.device_name, excluded_by_id[device_id])
                del pending[device_id]
                continue

            current = scan_by_id.get(device_id)
            if current is None:
                try:
                    current = self.client.get_device_health(device_id)
                except ApiError as e:
                    logger.warning('Could not re-check device %s (%s): %s',
                                    device_id, retry.device_name, e)
                    continue
            if current is None:
                logger.info('Device %s (%s) no longer exists -- clearing pending retry.',
                            device_id, retry.device_name)
                del pending[device_id]
                continue
            excluded, reason = is_excluded(current, self.config, unsupported_ids)
            if excluded:
                logger.info('Device %s (%s) is now excluded (%s) -- clearing pending retry without alerting.',
                            device_id, retry.device_name, reason)
                del pending[device_id]
                continue

            if current.timestamp > retry.pre_retry_timestamp:
                if current.status not in self.config.retry.failure_statuses:
                    logger.info('Device %s (%s) recovered after manual retrieval.',
                                device_id, retry.device_name)
                    resolved.append(retry)
                else:
                    logger.warning('Device %s (%s) retried and is still failing (%s).',
                                    device_id, retry.device_name, current.status)
                    # self.now, not current.timestamp: the ledger entry records
                    # when WE detected this, not FireMon's own data timestamp --
                    # which, for a device retried because it was stale, can
                    # already be long in the past and fall outside a digest's
                    # lookback window before it's ever reported.
                    alerts.append(AlertRecord(device_id, retry.device_name, ALERT_RETRY_FAILED,
                                               current.status, current.status_description,
                                               self.now))
                    carried_attempts[device_id] = retry.attempts
                del pending[device_id]
                continue

            grace = timedelta(minutes=self.config.retry.grace_period_minutes)
            elapsed = self.now - retry.triggered_at
            if elapsed > grace:
                logger.warning('Device %s (%s) triggered %s ago with no new retrieval observed.',
                                device_id, retry.device_name, elapsed)
                alerts.append(AlertRecord(device_id, retry.device_name, ALERT_RETRY_TIMEOUT,
                                           current.status, current.status_description,
                                           self.now))
                carried_attempts[device_id] = retry.attempts
                del pending[device_id]

        triggered = []
        for device_id, device in scan_by_id.items():
            if device_id in pending:
                continue
            if self._is_failure(device):
                reason = REASON_FAILURE_STATUS
            elif self._is_stale(device):
                reason = REASON_STALE
            else:
                continue

            if self.dry_run:
                logger.info('[dry-run] Device %s (%s) needs retrieval (%s, status=%s) -- '
                            'would trigger manual retrieval.',
                            device_id, device.name, reason, device.status)
                triggered.append(device)
                continue

            logger.info('Device %s (%s) needs retrieval (%s, status=%s) -- triggering manual retrieval.',
                        device_id, device.name, reason, device.status)
            try:
                self.client.trigger_manual_retrieval(device_id)
                pending[device_id] = PendingRetry(
                    device_id=device_id, device_name=device.name, reason=reason,
                    pre_retry_status=device.status, pre_retry_timestamp=device.timestamp,
                    triggered_at=self.now,
                    attempts=carried_attempts.get(device_id, 0) + 1,
                )
                triggered.append(device)
            except UnsupportedDeviceError as e:
                # Not a transient failure -- this device can never be
                # manually retrieved, full stop. Recorded once (this is the
                # only time it will ever reach this point: every future cycle
                # excludes it via `unsupported_ids` above) so the next digest
                # explains why, then excluded permanently.
                logger.warning('Device %s (%s) does not support manual retrieval -- '
                                'excluding it going forward: %s', device_id, device.name, e)
                unsupported[str(device_id)] = {
                    'name': device.name, 'detected_at': self.now.isoformat(), 'detail': str(e),
                }
                alerts.append(AlertRecord(device_id, device.name, ALERT_UNSUPPORTED_DEVICE,
                                           device.status, str(e), self.now))
            except ApiError as e:
                logger.error('Could not trigger manual retrieval for %s (%s): %s',
                              device_id, device.name, e)
                alerts.append(AlertRecord(device_id, device.name, ALERT_TRIGGER_ERROR,
                                           device.status, str(e), self.now))

        # A device can alert twice in one cycle -- e.g. "still failing" from the
        # pending recheck above, then its own immediate re-trigger also fails.
        # One ledger entry per device per cycle, most recent wins.
        deduped_alerts = {}
        for alert in alerts:
            deduped_alerts[alert.device_id] = alert
        alerts = list(deduped_alerts.values())

        if not self.dry_run:
            self.state.save_pending(pending)
            self.state.append_ledger(alerts)
            self.state.save_unsupported(unsupported)

        exit_code = EXIT_ALERTED if alerts else EXIT_OK
        return CycleResult(resolved=resolved, recorded=alerts,
                            triggered=triggered, exit_code=exit_code)
