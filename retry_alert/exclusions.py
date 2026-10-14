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
REASON_VIA_PARENT = 'retrieved_via_parent'
REASON_MANUAL = 'manual_device'


def matches_name_pattern(name, patterns):
    if not patterns:
        return False
    name_lower = name.lower()
    return any(p.lower() in name_lower for p in patterns if p)


def is_excluded(device, config, unsupported_ids, retrieval_targets=None):
    """Returns (excluded: bool, reason: str|None). `unsupported_ids` is a set
    of device ids learned at runtime (StateStore.load_unsupported) -- devices
    whose own data gives no advance signal that manual retrieval will fail,
    discovered only after trying it once. `retrieval_targets` is
    FireMonClient.get_retrieval_targets(): {child_id: (parent_id, name)} for
    devices whose retrieval is done by a parent device -- cloud assets/VNETs
    under a cloud-provider device, firewalls under a Firepower Management
    Center. Retrying such a child directly can only produce a retrieval
    error (the child has no retrieval of its own); the parent is what gets
    retried, and it is in the scan in its own right."""
    if config.retry.ignore_manual_devices and device.is_manual:
        return True, REASON_MANUAL
    if config.retry.ignore_orphaned and device.is_orphaned:
        return True, REASON_ORPHANED
    if config.retry.ignore_unlicensed and device.is_unlicensed:
        return True, REASON_UNLICENSED
    if matches_name_pattern(device.name, config.retry.exclude_name_contains):
        return True, REASON_NAME_PATTERN
    if device.id in unsupported_ids:
        return True, REASON_UNSUPPORTED
    if config.retry.ignore_child_devices and retrieval_targets and device.id in retrieval_targets:
        # Only children of the configured parent types (CLOUD_PROVIDER by
        # default). A DEVICE_MGR child -- a firewall under an FMC, Panorama
        # or Meraki -- is retrieved individually through its manager and
        # carries its own retrieval status, so it stays in scope. An unknown
        # parent type is treated as in scope: never exclude on a guess.
        parent_type = retrieval_targets[device.id][2] if len(retrieval_targets[device.id]) > 2 else ''
        if parent_type and parent_type.upper() in config.retry.ignore_children_of:
            return True, REASON_VIA_PARENT
    return False, None
