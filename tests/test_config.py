import os
import tempfile
import unittest
from pathlib import Path

from retry_alert.config import ConfigError, load_config

MINIMAL_INI = """
[firemon]
base_url = https://fm.example.invalid
username = svc-retry
password = filepassword

[smtp]
from_address = alerts@example.invalid
recipients = ops@example.invalid
"""

NO_PASSWORD_INI = """
[firemon]
base_url = https://fm.example.invalid
username = svc-retry

[smtp]
from_address = alerts@example.invalid
"""


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name, content):
        path = self.dir / name
        path.write_text(content, encoding='utf-8')
        return path

    def test_loads_minimal_config(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        self.assertEqual('https://fm.example.invalid', config.firemon.base_url)
        self.assertEqual('filepassword', config.firemon.password)
        self.assertEqual(['ops@example.invalid'], config.smtp.recipients)
        self.assertTrue(config.retry.state_dir.exists())

    def test_ignore_manual_devices_defaults_on_and_can_be_disabled(self):
        path = self._write('config.ini', MINIMAL_INI)
        self.assertTrue(load_config(path).retry.ignore_manual_devices)
        path = self._write('config3.ini', MINIMAL_INI + '\n[retry]\nignore_manual_devices = false\n')
        self.assertFalse(load_config(path).retry.ignore_manual_devices)

    def test_ignore_children_of_defaults_to_cloud_provider_and_is_upper_cased(self):
        path = self._write('config.ini', MINIMAL_INI)
        self.assertEqual(('CLOUD_PROVIDER',), load_config(path).retry.ignore_children_of)
        path = self._write('config4.ini', MINIMAL_INI + '\n[retry]\nignore_children_of = cloud_provider, device_mgr\n')
        self.assertEqual(('CLOUD_PROVIDER', 'DEVICE_MGR'), load_config(path).retry.ignore_children_of)

    def test_ignore_child_devices_defaults_on_and_can_be_disabled(self):
        path = self._write('config.ini', MINIMAL_INI)
        self.assertTrue(load_config(path).retry.ignore_child_devices)
        path = self._write('config2.ini', MINIMAL_INI + '\n[retry]\nignore_child_devices = false\n')
        self.assertFalse(load_config(path).retry.ignore_child_devices)

    def test_env_password_overrides_file(self):
        path = self._write('config.ini', MINIMAL_INI)
        os.environ['FMRETRY_PASSWORD'] = 'envpassword'
        self.addCleanup(os.environ.pop, 'FMRETRY_PASSWORD', None)
        config = load_config(path)
        self.assertEqual('envpassword', config.firemon.password)

    def test_missing_password_raises(self):
        path = self._write('config.ini', NO_PASSWORD_INI)
        os.environ.pop('FMRETRY_PASSWORD', None)
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config(self.dir / 'does-not-exist.ini')

    def test_relative_paths_resolve_against_config_dir(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        # Compare against the config file's own resolved parent, not self.dir
        # directly -- Path.resolve() can normalize short names/symlinks on
        # Windows, and both sides must go through the same transform.
        resolved_dir = path.resolve().parent
        self.assertEqual(resolved_dir / 'state', config.retry.state_dir)
        self.assertEqual(resolved_dir / 'templates', config.template_dir)

    def test_default_device_filter_is_management_station(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        self.assertIn('MANAGEMENT_STATION', config.firemon.device_filter)

    def test_blank_device_filter_means_no_filter_not_the_default(self):
        ini = MINIMAL_INI.replace('[firemon]', '[firemon]\ndevice_filter =')
        path = self._write('config.ini', ini)
        config = load_config(path)
        self.assertEqual('', config.firemon.device_filter)

    def test_blank_failure_statuses_means_empty_set_not_the_default(self):
        ini = MINIMAL_INI.replace('[firemon]', '[retry]\nfailure_statuses =\n\n[firemon]')
        path = self._write('config.ini', ini)
        config = load_config(path)
        self.assertEqual(set(), config.retry.failure_statuses)

    def test_ignore_orphaned_defaults_true(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        self.assertTrue(config.retry.ignore_orphaned)

    def test_ignore_orphaned_can_be_disabled(self):
        ini = MINIMAL_INI.replace('[firemon]', '[retry]\nignore_orphaned = false\n\n[firemon]')
        path = self._write('config.ini', ini)
        config = load_config(path)
        self.assertFalse(config.retry.ignore_orphaned)

    def test_ignore_unlicensed_defaults_true(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        self.assertTrue(config.retry.ignore_unlicensed)

    def test_ignore_unlicensed_can_be_disabled(self):
        ini = MINIMAL_INI.replace('[firemon]', '[retry]\nignore_unlicensed = false\n\n[firemon]')
        path = self._write('config.ini', ini)
        config = load_config(path)
        self.assertFalse(config.retry.ignore_unlicensed)

    def test_exclude_name_contains_defaults_to_empty(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        self.assertEqual((), config.retry.exclude_name_contains)

    def test_exclude_name_contains_parses_comma_separated_list(self):
        ini = MINIMAL_INI.replace('[firemon]', '[retry]\nexclude_name_contains = tgw-, spare-\n\n[firemon]')
        path = self._write('config.ini', ini)
        config = load_config(path)
        self.assertEqual(('tgw-', 'spare-'), config.retry.exclude_name_contains)

    def test_digest_interval_hours_defaults_to_24(self):
        path = self._write('config.ini', MINIMAL_INI)
        config = load_config(path)
        self.assertEqual(24, config.digest.interval_hours)

    def test_digest_interval_hours_can_be_overridden(self):
        ini = MINIMAL_INI.replace('[firemon]', '[digest]\ninterval_hours = 6\n\n[firemon]')
        path = self._write('config.ini', ini)
        config = load_config(path)
        self.assertEqual(6, config.digest.interval_hours)

    def test_empty_recipients_warns_but_does_not_raise(self):
        path = self._write('config.ini', MINIMAL_INI.replace(
            'recipients = ops@example.invalid', 'recipients ='))
        config = load_config(path)
        self.assertEqual([], config.smtp.recipients)
        self.assertTrue(any('recipients' in w for w in config.warnings))


if __name__ == '__main__':
    unittest.main()
