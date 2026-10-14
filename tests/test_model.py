import unittest

from retry_alert.model import DeviceHealth

# Shapes observed live against a real FireMon Security Manager device/health response.
LICENSED_DEVICE = {
    'id': 236, 'name': 'AMS-FW-1120-01', 'description': 'Discovered by Cisco FMC - 10.113.128.51',
    'licenses': ['SM'], 'revisionStatus': 'NORMALIZED', 'revisionCreateDate': '2026-01-01T00:00:00.000Z',
    'deviceStatuses': [{'retrievalStatus': 'SUCCESS', 'retrievalStatusDescription': 'OK',
                         'retrievalLastUpdated': '2026-01-01T00:00:00.000Z'}],
}

UNLICENSED_DEVICE = {
    'id': 413, 'name': 'Azure - China', 'description': '', 'licenses': None,
    'revisionStatus': 'RETRIEVAL_ERROR', 'revisionCreateDate': '2026-01-01T00:00:00.000Z',
    'deviceStatuses': [{'retrievalStatus': 'FAILURE', 'retrievalStatusDescription': 'not licensed',
                         'retrievalLastUpdated': '2026-01-01T00:00:00.000Z'}],
}

ORPHANED_DEVICE = {
    'id': 271, 'name': 'AWS-KFONE-USE1-FW-FTDV-01',
    'description': 'Orphaned by Cisco Firepower Management Centre', 'licenses': None,
    'revisionStatus': 'RETRIEVAL_ERROR', 'revisionCreateDate': '2026-01-01T00:00:00.000Z',
}


class TestDeviceHealthFromApi(unittest.TestCase):
    def test_licensed_device_is_not_flagged(self):
        device = DeviceHealth.from_api(LICENSED_DEVICE)
        self.assertEqual(('SM',), device.licenses)
        self.assertFalse(device.is_unlicensed)
        self.assertFalse(device.is_orphaned)
        self.assertEqual('SUCCESS', device.status)

    def test_null_licenses_field_is_unlicensed(self):
        device = DeviceHealth.from_api(UNLICENSED_DEVICE)
        self.assertEqual((), device.licenses)
        self.assertTrue(device.is_unlicensed)
        self.assertEqual('FAILURE', device.status)

    def test_orphaned_description_is_detected_case_insensitively(self):
        device = DeviceHealth.from_api(ORPHANED_DEVICE)
        self.assertTrue(device.is_orphaned)

        variant = dict(ORPHANED_DEVICE, description='ORPHANED BY something else')
        self.assertTrue(DeviceHealth.from_api(variant).is_orphaned)

    def test_missing_optional_fields_do_not_raise(self):
        device = DeviceHealth.from_api({'id': 1, 'name': 'bare'})
        self.assertEqual((), device.licenses)
        self.assertEqual('', device.description)
        self.assertTrue(device.is_unlicensed)
        self.assertFalse(device.is_orphaned)


if __name__ == '__main__':
    unittest.main()
