"""Compile the cost probe and validate bounds and native calibrations."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class FaultCostsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = shutil.which('cc')
        if not cc:
            raise unittest.SkipTest('C compiler unavailable')
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = pathlib.Path(cls.directory.name) / 'costs'
        flags = ['-static'] if sys.platform == 'linux' else []
        subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                        str(config.REPO / 'src/fault_costs.c'), '-o', str(cls.binary)], check=True)

    def test_bounds(self):
        for value in ('0', '-1', '513', '262145', '512x'):
            result = subprocess.run([str(self.binary), value], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, value)

    @unittest.skipUnless(sys.platform == 'linux', '4 KiB Linux native calibration')
    def test_native(self):
        result = subprocess.run([str(self.binary), '512'], check=True, capture_output=True,
                                text=True, timeout=30)
        row = json.loads(result.stdout)
        self.assertTrue(row['verified'])
        self.assertFalse(row['profile'])
        samples = {s['name']: s for s in row['samples']}
        for name in ('anon_first', 'anon_repeat', 'resident_zero'):
            self.assertEqual(samples[name]['operations'], 512)
            self.assertGreater(samples[name]['ns_per_op'], 0)
        if 'host_sigsegv_return' in samples:
            self.assertEqual(samples['host_sigsegv_return']['operations'], 20000)
            self.assertEqual(samples['host_mmap_32']['operations'], 16)
