from dataclasses import dataclass, field
from datetime import datetime, timezone


def parse_iso(date_str):
    """Parses a FireMon ISO timestamp; returns an aware datetime, or epoch if unparseable."""
    if not date_str:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


@dataclass
class DeviceHealth:
    id: int
    name: str
    status: str
    status_description: str
    timestamp: datetime
    description: str = ''
    licenses: tuple = field(default_factory=tuple)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_orphaned(self):
        # FireMon writes "Orphaned by <parent device>" into a device's own
        # description when it replaces/absorbs it -- an orphaned device is
        # already known-defunct, not actually broken, and will never resolve.
        return 'orphaned by' in self.description.lower()

    @property
    def is_unlicensed(self):
        # A device without the Security Manager license cannot actually be
        # managed/retrieved by design -- confirmed against a live fleet where
        # every device lacking 'SM' in its license list showed a permanent
        # RETRIEVAL_ERROR, while every device with 'SM' did not.
        return 'SM' not in self.licenses

    @classmethod
    def from_api(cls, raw):
        device_id = raw.get('id')
        name = raw.get('name') or f'device-{device_id}'
        description = raw.get('description') or ''
        licenses = tuple(raw.get('licenses') or ())
        revision_status = raw.get('revisionStatus') or 'UNKNOWN'
        timestamp = parse_iso(raw.get('revisionCreateDate'))
        status = revision_status
        status_description = ''

        statuses = raw.get('deviceStatuses') or []
        if statuses:
            latest = max(statuses, key=lambda s: parse_iso(s.get('retrievalLastUpdated')))
            latest_ts = parse_iso(latest.get('retrievalLastUpdated'))
            if latest_ts > timestamp:
                timestamp = latest_ts
            retrieval_status = latest.get('retrievalStatus')
            if retrieval_status:
                status = retrieval_status
            status_description = latest.get('retrievalStatusDescription') or ''

        return cls(id=device_id, name=name, status=status, status_description=status_description,
                    timestamp=timestamp, description=description, licenses=licenses, raw=raw)


# Why an issue is being retried/alerted on.
REASON_FAILURE_STATUS = 'failure_status'
REASON_STALE = 'stale'

# Why an alert is being sent, once a retry has run its course.
ALERT_RETRY_FAILED = 'retry_failed'
ALERT_RETRY_TIMEOUT = 'retry_timeout'
ALERT_TRIGGER_ERROR = 'trigger_error'
ALERT_UNSUPPORTED_DEVICE = 'unsupported_device'

ALERT_LABELS = {
    ALERT_RETRY_FAILED: 'Retry attempted, still failing',
    ALERT_RETRY_TIMEOUT: 'Retry triggered, no confirmation received',
    ALERT_TRIGGER_ERROR: 'Could not trigger manual retrieval',
    ALERT_UNSUPPORTED_DEVICE: "Doesn't support manual retrieval -- excluded going forward",
}


@dataclass
class PendingRetry:
    device_id: int
    device_name: str
    reason: str
    pre_retry_status: str
    pre_retry_timestamp: datetime
    triggered_at: datetime
    attempts: int = 1

    def to_json(self):
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'reason': self.reason,
            'pre_retry_status': self.pre_retry_status,
            'pre_retry_timestamp': self.pre_retry_timestamp.isoformat(),
            'triggered_at': self.triggered_at.isoformat(),
            'attempts': self.attempts,
        }

    @classmethod
    def from_json(cls, data):
        return cls(
            device_id=data['device_id'],
            device_name=data['device_name'],
            reason=data['reason'],
            pre_retry_status=data['pre_retry_status'],
            pre_retry_timestamp=parse_iso(data['pre_retry_timestamp']),
            triggered_at=parse_iso(data['triggered_at']),
            attempts=data.get('attempts', 1),
        )


@dataclass
class AlertRecord:
    device_id: int
    device_name: str
    alert_reason: str
    status: str
    status_description: str
    # When the engine detected/recorded this event -- NOT any timestamp
    # reported by the device itself. A device is commonly flagged precisely
    # BECAUSE its own data is old, so using that would push the entry outside
    # a digest's lookback window before it's ever reported.
    timestamp: datetime

    def to_json(self):
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'alert_reason': self.alert_reason,
            'status': self.status,
            'status_description': self.status_description,
            'timestamp': self.timestamp.isoformat(),
        }

    @classmethod
    def from_json(cls, data):
        return cls(
            device_id=data['device_id'],
            device_name=data['device_name'],
            alert_reason=data['alert_reason'],
            status=data['status'],
            status_description=data['status_description'],
            timestamp=parse_iso(data['timestamp']),
        )


# A device's status as of digest-compile time, re-checked live rather than
# just replayed from the ledger.
DIGEST_STILL_BROKEN = 'still_broken'
DIGEST_RESOLVED = 'resolved'
DIGEST_NOT_TRACKED = 'not_tracked'  # deleted, or now excluded (orphaned/unlicensed)

DIGEST_STATE_LABELS = {
    DIGEST_STILL_BROKEN: 'Still broken',
    DIGEST_RESOLVED: 'Resolved',
    DIGEST_NOT_TRACKED: 'No longer tracked',
}


@dataclass
class DigestDeviceSummary:
    """One device's row in a digest: every ledger event for it in the window,
    plus its live status as of digest-compile time."""
    device_id: int
    device_name: str
    event_count: int
    first_seen: datetime
    last_seen: datetime
    last_alert_reason: str
    last_status: str
    last_status_description: str
    current_state: str
