"""Check the large-window probe builds and verifies native Linux semantics."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class FaultWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if compiler is None:
            raise unittest.SkipTest('C compiler unavailable')
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = pathlib.Path(cls.directory.name) / 'window'
        flags = ['-static'] if sys.platform == 'linux' else []
        subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                        str(config.REPO / 'src/fault_window.c'), '-o', str(cls.binary)], check=True)

    def test_arguments(self):
        result = subprocess.run([str(self.binary), 'unexpected'], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux MADV_DONTNEED and 4 KiB pages')
    def test_native_semantics(self):
        result = subprocess.run([str(self.binary)], check=True, capture_output=True,
                                text=True, timeout=60)
        row = json.loads(result.stdout)
        self.assertTrue(row['verified'])
        self.assertEqual(row['sparse_resident_pages'], 257)
        self.assertEqual(row['guard_signals'], 3)
        self.assertTrue(row['cow'] and row['discard'] and row['recycled_zero'] and row['rlimit'])
