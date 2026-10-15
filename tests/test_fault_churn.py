"""Compile the probe on macOS; exercise Linux procfs through ./hd check on Linux."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class ChurnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if compiler is None:
            raise unittest.SkipTest('C compiler unavailable')
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = pathlib.Path(cls.directory.name) / 'fault_churn'
        flags = ['-static'] if sys.platform == 'linux' else []
        subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                        str(config.REPO / 'src/fault_churn.c'), '-o', str(cls.binary)], check=True)

    def test_arguments(self):
        subprocess.run([str(self.binary), 'child'], check=True, timeout=10)
        for args in (['0'], ['-1'], ['1junk'], ['1', '0'], ['1', '1', '65537'], ['7200', '1']):
            result = subprocess.run([str(self.binary), *args], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, args)

    @unittest.skipUnless(sys.platform == 'linux', 'procfs snapshots require Linux')
    def test_intervals(self):
        result = subprocess.run([str(self.binary), '2', '1', '32'], capture_output=True,
                                text=True, check=True, timeout=30)
        data = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(data['verified'])
        self.assertEqual(len(data['samples']), 2)
        self.assertEqual(data['schema'], 'hd.fault-churn/1')
        for sample in data['samples']:
            self.assertGreater(sample['self_maps'], 0)
            self.assertGreater(sample['mem_available_kb'], 0)
            self.assertTrue(all(v > 0 for v in sample['rates'].values()))
            cycles = sample['counts'][0] // 8
            self.assertEqual(sample['counts'], [cycles * 8, cycles * 256, cycles * 32, cycles])
            for k, rate in enumerate(sample['rates'].values()):
                self.assertAlmostEqual(rate, sample['counts'][k] * 1e9 / sample['work_ns'][k], delta=0.001)
