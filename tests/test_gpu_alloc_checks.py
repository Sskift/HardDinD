import copy
import gzip
import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gpu_alloc_checks as checks


class AllocationEvidenceTests(unittest.TestCase):
    def setUp(self):
        text = gzip.decompress((pathlib.Path(__file__).parent / 'fixtures/gpu-alloc-native.log.gz').read_bytes()).decode()
        self.native = checks.parse(text)

    def test_actual_native_observation_is_complete(self):
        self.assertTrue(self.native['probe_checks_pass'])
        self.assertEqual(checks.compare(self.native, self.native)['matched'], 57)

    def test_missing_duplicate_or_reordered_case_cannot_pass(self):
        for kind in ('missing', 'duplicate', 'reordered'):
            other = copy.deepcopy(self.native)
            if kind == 'missing':
                other['results'].pop()
            elif kind == 'duplicate':
                other['results'].append(other['results'][0])
            else:
                other['results'][0:2] = reversed(other['results'][0:2])
            self.assertFalse(checks.compare(self.native, other)['matches'])

    def test_errno_or_status_difference_cannot_pass(self):
        for key in ('errno', 'rc'):
            other = copy.deepcopy(self.native)
            other['results'][0][key] = -95
            self.assertFalse(checks.compare(self.native, other)['matches'])
        other = copy.deepcopy(self.native)
        row = other['results'][0]
        h = bytearray.fromhex(row['header_after'])
        struct.pack_into('<I', h, 28, 0xdeadbeef)
        row['header_after'] = h.hex()
        self.assertFalse(checks.compare(self.native, other)['matches'])

    def test_changed_pointer_is_not_hidden_by_address_normalization(self):
        for index, offset in ((0, 16), (46, 24)):
            other = copy.deepcopy(self.native)
            row = other['results'][index]
            h = bytearray.fromhex(row['header_after'])
            struct.pack_into('<Q', h, offset, struct.unpack_from('<Q', h, offset)[0] + 8)
            row['header_after'] = h.hex()
            self.assertFalse(checks.compare(self.native, other)['matches'])

    def test_missing_checks_or_end_cannot_pass(self):
        for key in ('checks', 'ends'):
            other = copy.deepcopy(self.native)
            other[key].pop()
            self.assertFalse(checks.compare(self.native, other)['matches'])

    def test_empty_baselines_cannot_pass_even_with_completion_flag(self):
        self.assertFalse(checks.compare({'observation_complete': True}, {'observation_complete': True})['matches'])

    def test_extended_finn_matrix_is_complete_and_not_interchangeable(self):
        text = gzip.decompress((pathlib.Path(__file__).parent / 'fixtures/gpu-alloc-native-v2.log.gz').read_bytes()).decode()
        extended = checks.parse(text)
        self.assertTrue(extended['probe_checks_pass'])
        self.assertEqual(checks.compare(extended, extended)['matched'], 74)
        self.assertFalse(checks.compare(self.native, extended)['matches'])
        self.assertFalse(checks.compare(extended, self.native)['matches'])
        self.assertFalse(checks.parse(text.replace('GPU_ALLOC_SCHEMA 2', 'GPU_ALLOC_SCHEMA 3'))['observation_complete'])
        self.assertFalse(checks.parse(text + 'GPU_ALLOC_SCHEMA 2\n')['observation_complete'])


if __name__ == '__main__':
    unittest.main()
