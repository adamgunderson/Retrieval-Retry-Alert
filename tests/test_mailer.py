import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from retry_alert.mailer import _group_by_state, build_digest_email
from retry_alert.model import (
    ALERT_RETRY_FAILED, ALERT_UNSUPPORTED_DEVICE,
    DIGEST_NOT_TRACKED, DIGEST_RESOLVED, DIGEST_STILL_BROKEN,
    DigestDeviceSummary,
)

from .support import make_smtp_config

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _summary(device_id, name, state, reason=ALERT_RETRY_FAILED, event_count=1):
    return DigestDeviceSummary(
        device_id=device_id, device_name=name, event_count=event_count,
        first_seen=NOW, last_seen=NOW, last_alert_reason=reason,
        last_status='RETRIEVAL_ERROR', last_status_description='', current_state=state,
    )


@dataclass
class _StubAppConfig:
    smtp: object
    template_dir: Path


class TestGroupByState(unittest.TestCase):
    def test_groups_in_fixed_order_regardless_of_input_order(self):
        summaries = [
            _summary(1, 'resolved-a', DIGEST_RESOLVED),
            _summary(2, 'broken-a', DIGEST_STILL_BROKEN),
            _summary(3, 'tracked-a', DIGEST_NOT_TRACKED),
            _summary(4, 'broken-b', DIGEST_STILL_BROKEN),
        ]
        groups = _group_by_state(summaries)
        states = [state for state, _items in groups]
        self.assertEqual([DIGEST_STILL_BROKEN, DIGEST_RESOLVED, DIGEST_NOT_TRACKED], states)
        still_broken_names = [s.device_name for s in dict(groups)[DIGEST_STILL_BROKEN]]
        self.assertEqual(['broken-a', 'broken-b'], still_broken_names)

    def test_empty_states_are_omitted_entirely(self):
        groups = _group_by_state([_summary(1, 'broken-a', DIGEST_STILL_BROKEN)])
        states = [state for state, _items in groups]
        self.assertEqual([DIGEST_STILL_BROKEN], states)

    def test_empty_input_produces_no_groups(self):
        self.assertEqual([], _group_by_state([]))


class TestBuildDigestEmail(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Empty template_dir -- falls back to the built-in default templates,
        # so this test exercises those, not whatever's in templates/ today.
        self.config = _StubAppConfig(smtp=make_smtp_config(), template_dir=Path(self._tmp.name))

    def test_subject_counts_still_broken_and_resolved(self):
        summaries = [_summary(1, 'a', DIGEST_STILL_BROKEN), _summary(2, 'b', DIGEST_RESOLVED)]
        subject, _html, _text = build_digest_email(self.config, summaries, NOW, NOW)
        self.assertIn('1 still broken', subject)
        self.assertIn('1 resolved', subject)

    def test_html_has_one_heading_per_nonempty_section(self):
        summaries = [_summary(1, 'broken-device', DIGEST_STILL_BROKEN),
                     _summary(2, 'resolved-device', DIGEST_RESOLVED)]
        _subject, html_body, _text = build_digest_email(self.config, summaries, NOW, NOW)
        self.assertIn('section-still_broken', html_body)
        self.assertIn('section-resolved', html_body)
        self.assertNotIn('section-not_tracked', html_body)
        self.assertIn('broken-device', html_body)
        self.assertIn('resolved-device', html_body)

    def test_text_has_one_heading_per_nonempty_section(self):
        summaries = [_summary(1, 'broken-device', DIGEST_STILL_BROKEN),
                     _summary(2, 'resolved-device', DIGEST_RESOLVED)]
        _subject, _html, text_body = build_digest_email(self.config, summaries, NOW, NOW)
        self.assertIn('STILL BROKEN', text_body)
        self.assertIn('RESOLVED', text_body)
        self.assertNotIn('NOT TRACKED', text_body)

    def test_device_name_is_html_escaped(self):
        summaries = [_summary(1, '<script>alert(1)</script>', DIGEST_STILL_BROKEN)]
        _subject, html_body, _text = build_digest_email(self.config, summaries, NOW, NOW)
        self.assertNotIn('<script>', html_body)
        self.assertIn('&lt;script&gt;', html_body)

    def test_unsupported_device_gets_its_own_reason_label(self):
        # Confirms the fix: this must NOT read as the generic "Could not
        # trigger manual retrieval" (ALERT_TRIGGER_ERROR) a transient failure
        # would show -- it's a distinct, permanent, "don't bother retrying"
        # fact about the device.
        summaries = [_summary(1, 'ilb-a', DIGEST_NOT_TRACKED, reason=ALERT_UNSUPPORTED_DEVICE)]
        _subject, html_body, text_body = build_digest_email(self.config, summaries, NOW, NOW)
        self.assertIn("doesn&#x27;t support manual retrieval", html_body.lower())
        self.assertIn("doesn't support manual retrieval", text_body.lower())


if __name__ == '__main__':
    unittest.main()
