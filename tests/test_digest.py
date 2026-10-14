import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from retry_alert.digest import build_digest, is_due, time_until_due
from retry_alert.engine import StateStore
from retry_alert.model import (
    ALERT_RETRY_FAILED, ALERT_TRIGGER_ERROR, ALERT_UNSUPPORTED_DEVICE,
    DIGEST_NOT_TRACKED, DIGEST_RESOLVED, DIGEST_STILL_BROKEN,
    AlertRecord,
)

from .support import FakeClient, make_device, make_digest_config, make_retry_config

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@dataclass
class _StubConfig:
    retry: object
    digest: object


class DigestTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = StateStore(self._tmp.name)
        self.config = _StubConfig(retry=make_retry_config(self._tmp.name),
                                   digest=make_digest_config(interval_hours=24))

    def _event(self, device_id, name, when, reason=ALERT_RETRY_FAILED, status='RETRIEVAL_ERROR'):
        return AlertRecord(device_id=device_id, device_name=name, alert_reason=reason,
                            status=status, status_description='', timestamp=when)


class TestIsDue(DigestTestBase):
    def test_due_immediately_when_never_sent(self):
        self.assertTrue(is_due(self.state, self.config, NOW))

    def test_not_due_before_the_interval_elapses(self):
        self.state.save_digest_state({'last_sent': (NOW - timedelta(hours=1)).isoformat()})
        self.assertFalse(is_due(self.state, self.config, NOW))


class TestTimeUntilDue(DigestTestBase):
    def test_positive_remaining_before_due(self):
        self.state.save_digest_state({'last_sent': (NOW - timedelta(hours=20)).isoformat()})
        remaining = time_until_due(self.state, self.config, NOW)
        self.assertEqual(timedelta(hours=4), remaining)

    def test_zero_or_negative_once_due(self):
        self.state.save_digest_state({'last_sent': (NOW - timedelta(hours=25)).isoformat()})
        remaining = time_until_due(self.state, self.config, NOW)
        self.assertLessEqual(remaining, timedelta(0))

    def test_matches_is_due_at_the_boundary(self):
        self.state.save_digest_state({'last_sent': (NOW - timedelta(hours=24)).isoformat()})
        self.assertEqual(timedelta(0), time_until_due(self.state, self.config, NOW))
        self.assertTrue(is_due(self.state, self.config, NOW))

    def test_due_once_the_interval_elapses(self):
        self.state.save_digest_state({'last_sent': (NOW - timedelta(hours=25)).isoformat()})
        self.assertTrue(is_due(self.state, self.config, NOW))


class TestBuildDigest(DigestTestBase):
    def test_empty_ledger_produces_nothing(self):
        summaries, since = build_digest(FakeClient([]), self.state, self.config, NOW)
        self.assertEqual([], summaries)
        self.assertEqual(NOW - timedelta(hours=24), since)

    def test_still_failing_device_is_reported_still_broken(self):
        self.state.append_ledger([self._event(1, 'fw-a', NOW - timedelta(hours=2))])
        client = FakeClient([make_device(1, 'fw-a', status='RETRIEVAL_ERROR')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(1, len(summaries))
        self.assertEqual(DIGEST_STILL_BROKEN, summaries[0].current_state)
        self.assertEqual(1, summaries[0].event_count)

    def test_recovered_device_is_reported_resolved(self):
        self.state.append_ledger([self._event(2, 'fw-b', NOW - timedelta(hours=2))])
        client = FakeClient([make_device(2, 'fw-b', status='SUCCESS')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(DIGEST_RESOLVED, summaries[0].current_state)

    def test_deleted_device_is_reported_not_tracked(self):
        self.state.append_ledger([self._event(3, 'fw-c', NOW - timedelta(hours=2))])
        client = FakeClient([])  # device 3 no longer appears in a scan at all

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(DIGEST_NOT_TRACKED, summaries[0].current_state)

    def test_now_orphaned_device_is_reported_not_tracked(self):
        self.state.append_ledger([self._event(4, 'fw-d', NOW - timedelta(hours=2))])
        client = FakeClient([make_device(4, 'fw-d', status='RETRIEVAL_ERROR',
                                          description='Orphaned by Cisco Firepower Management Centre')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(DIGEST_NOT_TRACKED, summaries[0].current_state)

    def test_name_pattern_excluded_device_is_reported_not_tracked(self):
        self.config.retry.exclude_name_contains = ('tgw-',)
        self.state.append_ledger([self._event(8, 'tgw-us-east-1', NOW - timedelta(hours=2))])
        client = FakeClient([make_device(8, 'tgw-us-east-1', status='RETRIEVAL_ERROR')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(DIGEST_NOT_TRACKED, summaries[0].current_state)

    def test_learned_unsupported_device_is_reported_not_tracked_not_resolved(self):
        # This is the exact scenario a live device surfaced: the trigger
        # failed with "does not support manual retrieval", the device's own
        # data is perfectly healthy (NORMALIZED), and without the exclusion
        # check this would misleadingly show as "Resolved" rather than
        # "excluded -- there was never anything to resolve."
        self.state.append_ledger([self._event(9, 'ilb-a', NOW - timedelta(hours=2),
                                                reason=ALERT_UNSUPPORTED_DEVICE, status='NORMALIZED')])
        self.state.save_unsupported({'9': {'name': 'ilb-a', 'detected_at': NOW.isoformat(), 'detail': 'x'}})
        client = FakeClient([make_device(9, 'ilb-a', status='NORMALIZED')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(DIGEST_NOT_TRACKED, summaries[0].current_state)

    def test_entry_exactly_at_the_last_digest_time_is_not_reported_again(self):
        # Regression: AlertRecord.timestamp and the prune cutoff are both
        # `now` from the cycle that recorded+reported them, so a boundary
        # entry sharing that exact instant with `last_sent` must NOT be
        # re-included, or a successfully reported device repeats forever.
        self.state.save_digest_state({'last_sent': NOW.isoformat()})
        self.state.append_ledger([self._event(7, 'fw-boundary', NOW)])  # exactly at last_sent
        client = FakeClient([make_device(7, 'fw-boundary', status='RETRIEVAL_ERROR')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual([], summaries)

    def test_entries_before_the_window_are_excluded(self):
        self.state.save_digest_state({'last_sent': (NOW - timedelta(hours=6)).isoformat()})
        self.state.append_ledger([self._event(5, 'fw-e', NOW - timedelta(hours=10))])  # before the window
        client = FakeClient([make_device(5, 'fw-e', status='RETRIEVAL_ERROR')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual([], summaries)

    def test_multiple_events_for_one_device_are_grouped(self):
        self.state.append_ledger([
            self._event(6, 'fw-f', NOW - timedelta(hours=3), reason=ALERT_TRIGGER_ERROR),
            self._event(6, 'fw-f', NOW - timedelta(hours=1), reason=ALERT_RETRY_FAILED),
        ])
        client = FakeClient([make_device(6, 'fw-f', status='RETRIEVAL_ERROR')])

        summaries, _ = build_digest(client, self.state, self.config, NOW)

        self.assertEqual(1, len(summaries))
        self.assertEqual(2, summaries[0].event_count)
        self.assertEqual(NOW - timedelta(hours=3), summaries[0].first_seen)
        self.assertEqual(NOW - timedelta(hours=1), summaries[0].last_seen)
        # Grouping keeps chronological order, so the most recent event's reason wins.
        self.assertEqual(ALERT_RETRY_FAILED, summaries[0].last_alert_reason)

    def test_live_scan_failure_falls_back_gracefully(self):
        self.state.append_ledger([self._event(7, 'fw-g', NOW - timedelta(hours=2))])
        client = FakeClient([make_device(7, 'fw-g', status='RETRIEVAL_ERROR')])
        client.list_should_fail = True

        summaries, since = build_digest(client, self.state, self.config, NOW)

        # Does not raise, still reports the device from ledger history alone.
        self.assertEqual(1, len(summaries))
        self.assertEqual(7, summaries[0].device_id)


if __name__ == '__main__':
    unittest.main()
