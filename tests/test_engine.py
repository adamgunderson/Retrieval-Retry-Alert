import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from retry_alert.engine import EXIT_ALERTED, EXIT_OK, RetryAlertEngine, StateStore
from retry_alert.model import (
    ALERT_RETRY_FAILED, ALERT_RETRY_TIMEOUT, ALERT_TRIGGER_ERROR, ALERT_UNSUPPORTED_DEVICE,
    AlertRecord, PendingRetry,
)

from .support import FakeClient, make_device, make_retry_config


@dataclass
class _StubConfig:
    retry: object


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = StateStore(self._tmp.name)
        self.retry_cfg = make_retry_config(self._tmp.name)
        self.config = _StubConfig(retry=self.retry_cfg)

    def engine(self, client, now=NOW, dry_run=False):
        return RetryAlertEngine(client, self.config, self.state, now=now, dry_run=dry_run)


class TestNewFailures(EngineTestBase):
    def test_failing_device_is_retried_not_alerted(self):
        device = make_device(1, 'fw-a', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([1], client.triggered)
        self.assertEqual([], result.recorded)
        self.assertEqual(EXIT_OK, result.exit_code)
        pending = self.state.load_pending()
        self.assertIn(1, pending)
        self.assertEqual('failure_status', pending[1].reason)

    def test_stale_device_is_retried(self):
        device = make_device(2, 'fw-b', status='SUCCESS', timestamp=NOW - timedelta(hours=30))
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([2], client.triggered)
        pending = self.state.load_pending()
        self.assertEqual('stale', pending[2].reason)

    def test_healthy_recent_device_is_left_alone(self):
        device = make_device(3, 'fw-c', status='SUCCESS', timestamp=NOW - timedelta(minutes=5))
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([], client.triggered)
        self.assertEqual({}, self.state.load_pending())
        self.assertEqual(EXIT_OK, result.exit_code)

    def test_orphaned_device_is_ignored_by_default(self):
        device = make_device(5, 'fw-e', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1),
                              description='Orphaned by Cisco Firepower Management Centre')
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([], client.triggered)
        self.assertEqual({}, self.state.load_pending())
        self.assertEqual([], result.triggered)

    def test_orphaned_device_is_retried_when_ignore_disabled(self):
        self.retry_cfg.ignore_orphaned = False
        device = make_device(6, 'fw-f2', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1),
                              description='Orphaned by Cisco Firepower Management Centre')
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([6], client.triggered)

    def test_unlicensed_device_is_ignored_by_default(self):
        device = make_device(7, 'fw-g2', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1),
                              licenses=())
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([], client.triggered)
        self.assertEqual({}, self.state.load_pending())
        self.assertEqual([], result.triggered)

    def test_unlicensed_device_is_retried_when_ignore_disabled(self):
        self.retry_cfg.ignore_unlicensed = False
        device = make_device(8, 'fw-h2', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1),
                              licenses=())
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([8], client.triggered)

    def test_name_pattern_excluded_device_is_ignored(self):
        self.retry_cfg.exclude_name_contains = ('tgw-',)
        device = make_device(9, 'tgw-us-east-1', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([], client.triggered)
        self.assertEqual({}, self.state.load_pending())
        self.assertEqual([], result.triggered)

    def test_name_pattern_only_matches_configured_pattern(self):
        self.retry_cfg.exclude_name_contains = ('tgw-',)
        device = make_device(10, 'fw-not-excluded', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])

        result = self.engine(client).run_cycle()

        self.assertEqual([10], client.triggered)

    def test_child_retrieved_via_parent_is_not_retried_but_parent_is(self):
        # Azure children (assets, VNETs) and FMC-managed firewalls carry
        # retrievalTargetDeviceId = parent. Retrying the child directly is
        # accepted by FireMon but can only fail; the parent is what retrieves.
        parent = make_device(414, 'Azure - KFD DEV', status='SUCCESS',
                             timestamp=NOW - timedelta(hours=30))      # stale -> retried
        child = make_device(500, 'AzReporting01', status='RETRIEVAL_ERROR',
                            timestamp=NOW - timedelta(hours=1))        # failing, but a child
        client = FakeClient([parent, child])
        client.retrieval_targets = {500: (414, parent.name)}

        result = self.engine(client).run_cycle()

        self.assertEqual([414], client.triggered)
        pending = self.state.load_pending()
        self.assertIn(414, pending)
        self.assertNotIn(500, pending)
        self.assertEqual([], result.recorded)

    def test_child_exclusion_can_be_switched_off(self):
        self.retry_cfg.ignore_child_devices = False
        child = make_device(500, 'AzReporting01', status='RETRIEVAL_ERROR',
                            timestamp=NOW - timedelta(hours=1))
        client = FakeClient([child])
        client.retrieval_targets = {500: (414, 'parent')}

        self.engine(client).run_cycle()

        self.assertEqual([500], client.triggered)

    def test_device_list_failure_aborts_the_cycle_like_a_scan_failure(self):
        # Without the parent/child map the engine would retry every child
        # and manufacture the errors it then alerts on. Fail closed.
        client = FakeClient([make_device(1, 'fw-a', status='RETRIEVAL_ERROR',
                                          timestamp=NOW - timedelta(hours=1))])
        client.targets_should_fail = True
        from retry_alert.api import ApiError
        with self.assertRaises(ApiError):
            self.engine(client).run_cycle()
        self.assertEqual([], client.triggered)

    def test_trigger_failure_is_recorded_immediately(self):
        device = make_device(4, 'fw-d', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])
        client.trigger_should_fail = {4}

        result = self.engine(client).run_cycle()

        self.assertEqual(1, len(result.recorded))
        self.assertEqual(ALERT_TRIGGER_ERROR, result.recorded[0].alert_reason)
        self.assertNotIn(4, self.state.load_pending())
        self.assertEqual(EXIT_ALERTED, result.exit_code)

    def test_recorded_timestamp_is_now_not_the_device_own_stale_timestamp(self):
        # Regression: a device is commonly flagged as stale/failing BECAUSE
        # its own FireMon timestamp is old -- that's the whole trigger
        # condition. If the ledger entry recorded that old timestamp instead
        # of "when we detected this," a digest filtering by "since the last
        # digest" would exclude it immediately, and the next prune would
        # delete it having never been reported at all.
        ancient = NOW - timedelta(days=90)
        device = make_device(13, 'fw-ancient', status='RETRIEVAL_ERROR', timestamp=ancient)
        client = FakeClient([device])
        client.trigger_should_fail = {13}

        result = self.engine(client).run_cycle()

        self.assertEqual(1, len(result.recorded))
        self.assertEqual(NOW, result.recorded[0].timestamp)
        ledger = self.state.read_ledger()
        self.assertEqual(NOW, ledger[0].timestamp)


class TestPendingRetries(EngineTestBase):
    def _seed_pending(self, device_id, name, reason='failure_status',
                       pre_status='RETRIEVAL_ERROR', pre_ts=None, triggered_at=None):
        pending = {device_id: PendingRetry(
            device_id=device_id, device_name=name, reason=reason,
            pre_retry_status=pre_status, pre_retry_timestamp=pre_ts or (NOW - timedelta(hours=2)),
            triggered_at=triggered_at or (NOW - timedelta(minutes=5)),
        )}
        self.state.save_pending(pending)

    def test_recovered_device_clears_pending_without_alert(self):
        self._seed_pending(10, 'fw-e')
        client = FakeClient([make_device(10, 'fw-e', status='SUCCESS', timestamp=NOW - timedelta(minutes=1))])

        result = self.engine(client).run_cycle()

        self.assertEqual(1, len(result.resolved))
        self.assertEqual([], result.recorded)
        self.assertNotIn(10, self.state.load_pending())

    def test_pending_device_excluded_after_trigger_is_dropped_without_alert(self):
        # Operator added the device's name to exclude_name_contains AFTER a
        # retry was already triggered for it. The next cycle must not alert
        # "still failing" on a device it was just told to stop watching, must
        # not re-fetch it, and must not re-trigger it.
        self._seed_pending(14, 'USE-FW-ASAV30-03-KFDEV')
        self.retry_cfg.exclude_name_contains = ('tgw-', 'USE-FW-ASAV30-03-KFDEV')
        client = FakeClient([make_device(14, 'USE-FW-ASAV30-03-KFDEV', status='RETRIEVAL_ERROR',
                                          timestamp=NOW - timedelta(minutes=1))])

        result = self.engine(client).run_cycle()

        self.assertEqual([], result.recorded)
        self.assertEqual([], result.resolved)
        self.assertEqual([], client.triggered)
        self.assertEqual([], client.health_lookup_calls)
        self.assertNotIn(14, self.state.load_pending())
        self.assertEqual(EXIT_OK, result.exit_code)

    def test_pending_device_absent_from_scan_but_excluded_on_refetch_is_dropped(self):
        # The device isn't in the scan at all (e.g. the SIQL device_filter
        # changed), and the per-device re-fetch shows it's now excluded.
        self._seed_pending(15, 'tgw-late')
        self.retry_cfg.exclude_name_contains = ('tgw-',)
        client = FakeClient([])
        late = make_device(15, 'tgw-late', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(minutes=1))
        client.get_device_health = lambda device_id, _d=late: _d if device_id == 15 else None

        result = self.engine(client).run_cycle()

        self.assertEqual([], result.recorded)
        self.assertEqual([], client.triggered)
        self.assertNotIn(15, self.state.load_pending())

    def test_still_failing_after_retry_is_recorded(self):
        self._seed_pending(11, 'fw-f')
        client = FakeClient([make_device(11, 'fw-f', status='RETRIEVAL_ERROR',
                                          timestamp=NOW - timedelta(minutes=1))])

        result = self.engine(client).run_cycle()

        self.assertEqual(1, len(result.recorded))
        self.assertEqual(ALERT_RETRY_FAILED, result.recorded[0].alert_reason)
        # Still unhealthy, so a fresh retry is queued in the same cycle --
        # retries keep going every cycle regardless of digest timing.
        pending = self.state.load_pending()
        self.assertIn(11, pending)
        self.assertEqual(2, pending[11].attempts)
        self.assertEqual([11], client.triggered)

    def test_no_confirmation_within_grace_period_is_recorded(self):
        self._seed_pending(12, 'fw-g', triggered_at=NOW - timedelta(minutes=30))
        # Timestamp unchanged since the retry was triggered.
        client = FakeClient([make_device(12, 'fw-g', status='RETRIEVAL_ERROR',
                                          timestamp=NOW - timedelta(hours=2))])

        result = self.engine(client).run_cycle()

        self.assertEqual(1, len(result.recorded))
        self.assertEqual(ALERT_RETRY_TIMEOUT, result.recorded[0].alert_reason)
        # Device 12 is still in a failure status, so it's re-queued immediately.
        pending = self.state.load_pending()
        self.assertIn(12, pending)
        self.assertEqual(2, pending[12].attempts)

    def test_within_grace_period_stays_pending(self):
        self._seed_pending(13, 'fw-h', triggered_at=NOW - timedelta(minutes=5))
        client = FakeClient([make_device(13, 'fw-h', status='RETRIEVAL_ERROR',
                                          timestamp=NOW - timedelta(hours=2))])

        result = self.engine(client).run_cycle()

        self.assertEqual([], result.recorded)
        self.assertIn(13, self.state.load_pending())


class TestLedger(EngineTestBase):
    def test_recorded_events_are_appended_to_the_ledger(self):
        device = make_device(40, 'fw-l', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])
        client.trigger_should_fail = {40}

        self.engine(client).run_cycle()
        self.engine(client, now=NOW + timedelta(minutes=1)).run_cycle()

        ledger = self.state.read_ledger()
        self.assertEqual(2, len(ledger))
        self.assertTrue(all(e.device_id == 40 for e in ledger))

    def test_prune_ledger_removes_entries_exactly_at_the_cutoff(self):
        # Regression: prune_ledger(before) is always called with before=now,
        # the SAME value AlertRecord.timestamp was just stamped with -- a >=
        # comparison would never drop those entries. Must be a strict >.
        self.state.append_ledger([AlertRecord(1, 'fw-x', ALERT_TRIGGER_ERROR, 'FAILURE', '', NOW)])
        self.state.prune_ledger(NOW)
        self.assertEqual([], self.state.read_ledger())

    def test_prune_ledger_keeps_entries_strictly_after_the_cutoff(self):
        self.state.append_ledger([AlertRecord(2, 'fw-y', ALERT_TRIGGER_ERROR, 'FAILURE', '',
                                               NOW + timedelta(seconds=1))])
        self.state.prune_ledger(NOW)
        self.assertEqual(1, len(self.state.read_ledger()))

    def test_duplicate_alert_for_same_device_in_one_cycle_is_deduped(self):
        # Device is pending, retried, still failing (-> event #1), and its
        # immediate same-cycle re-trigger also fails (-> event #2). Only one
        # ledger entry for the device should be appended this cycle.
        pending = {41: PendingRetry(
            device_id=41, device_name='fw-m', reason='failure_status',
            pre_retry_status='RETRIEVAL_ERROR', pre_retry_timestamp=NOW - timedelta(hours=2),
            triggered_at=NOW - timedelta(minutes=5),
        )}
        self.state.save_pending(pending)
        client = FakeClient([make_device(41, 'fw-m', status='RETRIEVAL_ERROR',
                                          timestamp=NOW - timedelta(minutes=1))])
        client.trigger_should_fail = {41}

        result = self.engine(client).run_cycle()

        device_ids = [a.device_id for a in result.recorded]
        self.assertEqual([41], device_ids)
        self.assertEqual(1, len(self.state.read_ledger()))


class TestUnsupportedDevices(EngineTestBase):
    def test_first_encounter_is_recorded_and_excludes_going_forward(self):
        device = make_device(60, 'ilb-a', status='SUCCESS', timestamp=NOW - timedelta(hours=30))
        client = FakeClient([device])
        client.trigger_unsupported = {60}

        result = self.engine(client).run_cycle()

        self.assertEqual([60], client.triggered)
        self.assertNotIn(60, self.state.load_pending())
        self.assertEqual(1, len(result.recorded))
        self.assertEqual(ALERT_UNSUPPORTED_DEVICE, result.recorded[0].alert_reason)
        unsupported = self.state.load_unsupported()
        self.assertIn('60', unsupported)

    def test_known_unsupported_device_is_never_retried_again(self):
        device = make_device(61, 'ilb-b', status='SUCCESS', timestamp=NOW - timedelta(hours=30))
        client = FakeClient([device])
        client.trigger_unsupported = {61}

        self.engine(client).run_cycle()
        self.assertEqual([61], client.triggered)

        second = self.engine(client, now=NOW + timedelta(hours=1)).run_cycle()

        self.assertEqual([61], client.triggered)  # unchanged -- never attempted again
        self.assertEqual([], second.recorded)      # and not re-reported either

    def test_dry_run_never_discovers_or_persists_unsupported(self):
        device = make_device(62, 'ilb-c', status='SUCCESS', timestamp=NOW - timedelta(hours=30))
        client = FakeClient([device])
        client.trigger_unsupported = {62}

        self.engine(client, dry_run=True).run_cycle()

        self.assertEqual([], client.triggered)
        self.assertEqual({}, self.state.load_unsupported())


class TestDryRun(EngineTestBase):
    def test_dry_run_does_not_trigger_or_persist_state(self):
        device = make_device(30, 'fw-k', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])

        result = self.engine(client, dry_run=True).run_cycle()

        self.assertEqual([], client.triggered)
        self.assertEqual({}, self.state.load_pending())
        self.assertEqual(1, len(result.triggered))

    def test_dry_run_does_not_append_ledger(self):
        device = make_device(31, 'fw-k2', status='RETRIEVAL_ERROR', timestamp=NOW - timedelta(hours=1))
        client = FakeClient([device])
        client.trigger_should_fail = {31}

        self.engine(client, dry_run=True).run_cycle()

        self.assertEqual([], self.state.read_ledger())


if __name__ == '__main__':
    unittest.main()
