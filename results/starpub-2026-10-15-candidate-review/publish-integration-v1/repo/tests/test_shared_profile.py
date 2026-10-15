import importlib.util
import json
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('shared_profile',
    pathlib.Path(__file__).resolve().parents[1] / 'scripts/analyze-shared-profile.py')
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class SharedProfileTests(unittest.TestCase):
    def test_partial_duplicate_and_impossible_durations_are_rejected(self):
        row = dict(opcode=33, lock_type=2, count=10, attempts=12,
                   queue_ns=100, service_ns=200, reply_ns=100, total_ns=1000, max_total_ns=500)
        line = 'HARDDIND_REQUEST_PROFILE ' + json.dumps(row) + '\n'
        end = 'HARDDIND_REQUEST_PROFILE_END rows=1\n'
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'profile.log'
            path.write_text(line + end)
            self.assertEqual(profile.read_profile(path)['rows'][0]['average_us']['total'], .1)
            for bad in [line, line + end + end, line + line + end,
                        line.replace('"service_ns": 200', '"service_ns": 2000') + end,
                        line.replace('"attempts": 12', '"attempts": 9') + end]:
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    profile.read_profile(path)
