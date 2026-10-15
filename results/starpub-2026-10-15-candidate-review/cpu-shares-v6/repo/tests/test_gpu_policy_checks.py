import copy
import gzip
import importlib.util
import json
import pathlib
import struct
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("policy", ROOT / "scripts/gpu_policy_checks.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
LOG = gzip.decompress((ROOT / "tests/fixtures/gpu-policy-native.log.gz").read_bytes()).decode()


class PolicyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.native = policy.parse(LOG)
        self.assertTrue(self.native['probe_checks_pass'])

    def test_missing_or_duplicate_occurrences_cannot_pass(self):
        lines = LOG.splitlines()
        first = next(i for i, line in enumerate(lines) if line.startswith('GPU_SHARE_RESULT '))
        missing = lines[:first] + lines[first + 1:]
        self.assertFalse(policy.parse('\n'.join(missing))['observation_complete'])
        duplicate = lines[:first] + [lines[first]] + lines[first:]
        self.assertFalse(policy.parse('\n'.join(duplicate))['observation_complete'])

    def test_failed_or_duplicate_end_is_not_pass(self):
        failed = LOG.replace('GPU_SHARE_END status=0', 'GPU_SHARE_END status=1')
        self.assertFalse(policy.parse(failed)['probe_checks_pass'])
        duplicate = LOG + 'GPU_SHARE_END status=0 internal_cuda_verified=0\n'
        self.assertFalse(policy.parse(duplicate)['observation_complete'])

    def test_inherited_fd_failure_is_compared(self):
        other = copy.deepcopy(self.native)
        other['results'][-1]['ok'] = False
        self.assertFalse(policy.compare(self.native, other)['matches'])

    def test_changed_header_and_child_bytes_are_compared(self):
        for field, offset in [('header_after', 8), ('child_after', 11)]:
            other = copy.deepcopy(self.native)
            data = bytearray.fromhex(other['results'][0][field]); data[offset] ^= 1
            other['results'][0][field] = data.hex()
            self.assertFalse(policy.compare(self.native, other)['matches'])

    def test_changed_pointer_is_not_erased_by_address_normalization(self):
        other = copy.deepcopy(self.native)
        data = bytearray.fromhex(other['results'][0]['header_after'])
        ptr = struct.unpack_from('<Q', data, 16)[0]
        struct.pack_into('<Q', data, 16, ptr + 4096)
        other['results'][0]['header_after'] = data.hex()
        self.assertFalse(policy.compare(self.native, other)['matches'])

    def test_missing_identity_or_failed_checks_are_not_pass(self):
        absent = '\n'.join(l for l in LOG.splitlines() if not l.startswith('GPU_SHARE_IDENTITIES '))
        self.assertFalse(policy.parse(absent)['observation_complete'])
        failed = LOG.replace('GPU_SHARE_CHECK open_control pass', 'GPU_SHARE_CHECK open_control fail')
        self.assertFalse(policy.parse(failed)['probe_checks_pass'])

    def test_schema_needs_all_additional_observations(self):
        self.assertFalse(policy.parse('GPU_SHARE_SCHEMA 2\n' + LOG)['observation_complete'])
        self.assertFalse(policy.parse('GPU_SHARE_SCHEMA 3\n' + LOG)['observation_complete'])

    def test_same_raw_observations_match(self):
        self.assertTrue(policy.compare(self.native, copy.deepcopy(self.native))['matches'])
