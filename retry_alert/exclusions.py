"""Shared "should we even consider this device" logic.

Used by both the retry engine (to skip these devices before ever attempting a
retry) and the digest (to report them as "no longer tracked" rather than
resolved/still-broken, since a device that's permanently excluded was never
actually fixed -- it's just not being watched any more). Keeping this in one
place means a device excluded by the engine is guaranteed to be classified
the same way when the digest re-checks it live.
"""

REASON_ORPHANED = 'orphaned'
REASON_UNLICENSED = 'unlicensed'
REASON_NAME_PATTERN = 'name_pattern'
REASON_UNSUPPORTED = 'unsupported'


def matches_name_pattern(name, patterns):
    if not patterns:
        return False
    name_lower = name.lower()
    return any(p.lower() in name_lower for p in patterns if p)


def is_excluded(device, config, unsupported_ids):
    """Returns (excluded: bool, reason: str|None). `unsupported_ids` is a set
    of device ids learned at runtime (StateStore.load_unsupported) -- devices
    whose own data gives no advance signal that manual retrieval will fail,
    discovered only after trying it once."""
    if config.retry.ignore_orphaned and device.is_orphaned:
        return True, REASON_ORPHANED
    if config.retry.ignore_unlicensed and device.is_unlicensed:
        return True, REASON_UNLICENSED
    if matches_name_pattern(device.name, config.retry.exclude_name_contains):
        return True, REASON_NAME_PATTERN
    if device.id in unsupported_ids:
        return True, REASON_UNSUPPORTED
    return False, None
