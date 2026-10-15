"""Check the growing heap probe's bounds and native sparse residency."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class HeapChurnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if compiler is None:
            raise unittest.SkipTest('C compiler unavailable')
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = pathlib.Path(cls.directory.name) / 'fault_heap_churn'
        flags = ['-static'] if sys.platform == 'linux' else []
        subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                        str(config.REPO / 'src/fault_heap_churn.c'), '-o', str(cls.binary)], check=True)

    def test_arguments(self):
        subprocess.run([str(self.binary), 'child'], check=True, timeout=10)
        for args in (['0'], ['33'], ['1', '0'], ['1', '61'], ['1', '1', '65']):
            result = subprocess.run([str(self.binary), *args], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, args)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux residency semantics')
    def test_sparse_heap(self):
        result = subprocess.run([str(self.binary), '2', '1', '1'], check=True,
                                capture_output=True, text=True, timeout=30)
        row = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(row['verified'])
        self.assertEqual(len(row['samples']), 2)
        for i, sample in enumerate(row['samples'], 1):
            self.assertEqual(sample['retained_mib'], i)
            sparse = (1024 * 1024 // row['page_bytes'] + 31) // 32
            self.assertEqual(sample['resident_pages'], i * sparse)
            self.assertGreater(sample['rounds'], 0)
            self.assertTrue(all(v > 0 for v in sample['rates'].values()))
