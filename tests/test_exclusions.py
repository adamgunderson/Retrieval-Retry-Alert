import unittest
from dataclasses import dataclass

from retry_alert.exclusions import (
    REASON_NAME_PATTERN, REASON_ORPHANED, REASON_UNLICENSED, REASON_UNSUPPORTED,
    REASON_MANUAL, REASON_VIA_PARENT, is_excluded, matches_name_pattern,
)

from .support import make_device, make_retry_config


@dataclass
class _StubConfig:
    retry: object


class TestMatchesNamePattern(unittest.TestCase):
    def test_no_patterns_never_matches(self):
        self.assertFalse(matches_name_pattern('tgw-anything', ()))

    def test_substring_match_is_case_insensitive(self):
        self.assertTrue(matches_name_pattern('TGW-us-east-1', ('tgw-',)))
        self.assertTrue(matches_name_pattern('tgw-us-east-1', ('TGW-',)))

    def test_no_match(self):
        self.assertFalse(matches_name_pattern('fw-a', ('tgw-',)))

    def test_any_of_several_patterns_matches(self):
        self.assertTrue(matches_name_pattern('spare-switch-01', ('tgw-', 'spare-')))


class TestIsExcluded(unittest.TestCase):
    def setUp(self):
        self.retry_cfg = make_retry_config('/tmp/unused')
        self.config = _StubConfig(retry=self.retry_cfg)

    def test_not_excluded_by_default(self):
        device = make_device(1, 'fw-a')
        excluded, reason = is_excluded(device, self.config, set())
        self.assertFalse(excluded)
        self.assertIsNone(reason)

    def test_orphaned(self):
        device = make_device(2, 'fw-b', description='Orphaned by something')
        excluded, reason = is_excluded(device, self.config, set())
        self.assertTrue(excluded)
        self.assertEqual(REASON_ORPHANED, reason)

    def test_unlicensed(self):
        device = make_device(3, 'fw-c', licenses=())
        excluded, reason = is_excluded(device, self.config, set())
        self.assertTrue(excluded)
        self.assertEqual(REASON_UNLICENSED, reason)

    def test_name_pattern(self):
        self.retry_cfg.exclude_name_contains = ('tgw-',)
        device = make_device(4, 'tgw-us-east-1')
        excluded, reason = is_excluded(device, self.config, set())
        self.assertTrue(excluded)
        self.assertEqual(REASON_NAME_PATTERN, reason)

    def test_unsupported(self):
        device = make_device(5, 'fw-d')
        excluded, reason = is_excluded(device, self.config, {5})
        self.assertTrue(excluded)
        self.assertEqual(REASON_UNSUPPORTED, reason)

    def test_disabling_ignore_orphaned_stops_excluding_it(self):
        self.retry_cfg.ignore_orphaned = False
        device = make_device(6, 'fw-e', description='Orphaned by something')
        excluded, _reason = is_excluded(device, self.config, set())
        self.assertFalse(excluded)

    def test_retrieved_via_parent(self):
        # An Azure asset whose retrievalTargetDeviceId is its cloud-provider
        # parent: it has no retrieval of its own, so it is never retried.
        device = make_device(500, 'AzReporting01', status='RETRIEVAL_ERROR')
        targets = {500: (414, 'Azure - KFD DEV - GIO Infrastructure')}
        excluded, reason = is_excluded(device, self.config, set(), targets)
        self.assertTrue(excluded)
        self.assertEqual(REASON_VIA_PARENT, reason)

    def test_parent_itself_is_not_excluded_by_the_targets_map(self):
        parent = make_device(414, 'Azure - KFD DEV - GIO Infrastructure', status='RETRIEVAL_ERROR')
        targets = {500: (414, parent.name)}
        excluded, _reason = is_excluded(parent, self.config, set(), targets)
        self.assertFalse(excluded)

    def test_no_targets_map_means_nothing_is_parent_excluded(self):
        device = make_device(500, 'AzReporting01')
        self.assertFalse(is_excluded(device, self.config, set(), None)[0])
        self.assertFalse(is_excluded(device, self.config, set(), {})[0])

    def test_manual_device(self):
        # A synthetic router: managedType MANUAL, maintained by hand, no
        # collector, no retrieval job. Its 404 on trigger is permanent.
        device = make_device(297, 'KF-EUN-Transit-Gateway', managed_type='MANUAL')
        excluded, reason = is_excluded(device, self.config, set())
        self.assertTrue(excluded)
        self.assertEqual(REASON_MANUAL, reason)

    def test_managed_device_is_not_manual(self):
        device = make_device(298, 'fw-real', managed_type='MANAGED')
        self.assertFalse(is_excluded(device, self.config, set())[0])

    def test_disabling_ignore_manual_devices_stops_excluding_them(self):
        self.retry_cfg.ignore_manual_devices = False
        device = make_device(297, 'KF-EUN-Transit-Gateway', managed_type='MANUAL')
        self.assertFalse(is_excluded(device, self.config, set())[0])

    def test_disabling_ignore_child_devices_stops_excluding_them(self):
        self.retry_cfg.ignore_child_devices = False
        device = make_device(500, 'AzReporting01')
        targets = {500: (414, 'parent')}
        excluded, _reason = is_excluded(device, self.config, set(), targets)
        self.assertFalse(excluded)


if __name__ == '__main__':
    unittest.main()
