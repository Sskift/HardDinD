import copy
import gzip
import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gpu_feature_checks as checks


class SystemFeatureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.text = gzip.decompress((pathlib.Path(__file__).parent / 'fixtures/gpu-feature-native.log.gz').read_bytes()).decode()
        self.native = checks.parse(self.text)

    def test_native_fixture_and_error_statuses(self):
        self.assertTrue(self.native['probe_checks_pass'])
        self.assertEqual(checks.compare(self.native, self.native)['matched'], 31)
        for field in ('rc', 'errno'):
            changed = copy.deepcopy(self.native)
            changed['results'][0][field] = -95
            self.assertFalse(checks.compare(self.native, changed)['matches'])

    def test_missing_repeated_or_truncated_observations(self):
        for key in ('checks', 'results', 'identities', 'ends'):
            for duplicate in (False, True):
                changed = copy.deepcopy(self.native)
                if duplicate:
                    changed[key].append(changed[key][0])
                else:
                    changed[key].pop()
                self.assertFalse(checks.compare(self.native, changed)['matches'])
        malformed = self.text.replace(self.native['results'][0]['header_after'], '00')
        self.assertFalse(checks.parse(malformed)['observation_complete'])

    def test_changed_pointer_and_changed_mask_are_not_normalized_away(self):
        changed = copy.deepcopy(self.native)
        h = bytearray.fromhex(changed['results'][0]['header_after'])
        struct.pack_into('<Q', h, 16, struct.unpack_from('<Q', h, 16)[0] + 8)
        changed['results'][0]['header_after'] = h.hex()
        self.assertFalse(checks.compare(self.native, changed)['matches'])
        changed = copy.deepcopy(self.native)
        changed['results'][0]['child_after'] = 'ffffffff'
        self.assertFalse(checks.compare(self.native, changed)['matches'])

    def test_empty_baseline_and_failed_end_are_not_passes(self):
        fake = {'observation_complete': True}
        self.assertFalse(checks.compare(fake, fake)['matches'])
        failed = checks.parse(self.text.replace('END status=0', 'END status=1'))
        self.assertTrue(failed['observation_complete'])
        self.assertFalse(failed['probe_checks_pass'])


if __name__ == '__main__':
    unittest.main()
