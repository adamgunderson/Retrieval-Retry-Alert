import logging
from datetime import timedelta

from .api import ApiError
from .exclusions import is_excluded
from .model import (
    DIGEST_NOT_TRACKED, DIGEST_RESOLVED, DIGEST_STILL_BROKEN,
    DigestDeviceSummary, parse_iso,
)

logger = logging.getLogger(__name__)


def _since(state, config, now):
    digest_state = state.load_digest_state()
    last_sent = digest_state.get('last_sent')
    if last_sent:
        return parse_iso(last_sent)
    return now - timedelta(hours=config.digest.interval_hours)


def time_until_due(state, config, now):
    """How long until the next digest is due. <= timedelta(0) means due now."""
    since = _since(state, config, now)
    return (since + timedelta(hours=config.digest.interval_hours)) - now


def is_due(state, config, now):
    return time_until_due(state, config, now) <= timedelta(0)


def build_digest(client, state, config, now):
    """Returns (summaries, since): one DigestDeviceSummary per device with a
    ledger event since the last digest (or interval_hours ago, if none has
    ever been sent), each carrying its live status as of right now rather than
    just replaying what the ledger says happened. A device currently excluded
    (orphaned, unlicensed, name pattern, or learned not to support manual
    retrieval), or no longer answering at all, is reported as "no longer
    tracked" rather than "resolved" -- it was never actually fixed, it's just
    not being watched any more. The digest is a record of what happened, not
    filtered by today's exclusions.
    """
    since = _since(state, config, now)
    # Strictly after, matching StateStore.prune_ledger's own boundary: after a
    # successful send, nothing with timestamp <= since should remain in the
    # ledger at all, but this stays consistent even if that invariant is ever
    # violated (a hand-edited ledger, a bug) rather than silently re-including
    # a boundary entry.
    entries = [e for e in state.read_ledger() if e.timestamp > since]
    if not entries:
        return [], since

    by_device = {}
    for entry in entries:
        by_device.setdefault(entry.device_id, []).append(entry)

    try:
        current_by_id = {d.id: d for d in client.get_device_health_list()}
    except ApiError as e:
        logger.warning('Could not refresh live status for the digest -- reporting ledger history only: %s', e)
        current_by_id = None

    unsupported_ids = {int(k) for k in state.load_unsupported()}

    summaries = []
    for device_id, device_entries in by_device.items():
        device_entries.sort(key=lambda e: e.timestamp)
        first, last = device_entries[0], device_entries[-1]

        current_state = DIGEST_NOT_TRACKED
        if current_by_id is not None:
            current = current_by_id.get(device_id)
            if current is not None:
                excluded, _reason = is_excluded(current, config, unsupported_ids)
                if excluded:
                    current_state = DIGEST_NOT_TRACKED
                elif current.status in config.retry.failure_statuses:
                    current_state = DIGEST_STILL_BROKEN
                else:
                    current_state = DIGEST_RESOLVED

        summaries.append(DigestDeviceSummary(
            device_id=device_id, device_name=last.device_name, event_count=len(device_entries),
            first_seen=first.timestamp, last_seen=last.timestamp,
            last_alert_reason=last.alert_reason, last_status=last.status,
            last_status_description=last.status_description, current_state=current_state,
        ))

    # Still-broken devices first (what needs attention), then by how often
    # they flapped, then alphabetically.
    order = {DIGEST_STILL_BROKEN: 0, DIGEST_RESOLVED: 1, DIGEST_NOT_TRACKED: 2}
    summaries.sort(key=lambda s: (order[s.current_state], -s.event_count, s.device_name.lower()))
    return summaries, since
