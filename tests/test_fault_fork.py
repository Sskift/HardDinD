"""Bounds and verified fork/COW phases of the static probe."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class FaultForkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = shutil.which('cc')
        if not cc:
            raise unittest.SkipTest('C compiler unavailable')
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = pathlib.Path(cls.directory.name) / 'fork'
        subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror',
                        *(['-static'] if sys.platform == 'linux' else []),
                        str(config.REPO / 'src/fault_fork.c'), '-o', str(cls.binary)], check=True)

    def test_bounds(self):
        for args in (['0'], ['-1'], ['5'], ['1x'], ['1', '32'], ['1', '257']):
            result = subprocess.run([str(self.binary), *args], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, args)

    @unittest.skipUnless(sys.platform == 'linux', '4 KiB Linux fork/COW workload')
    def test_native(self):
        result = subprocess.run([str(self.binary), '1', '16'], check=True,
                                capture_output=True, text=True, timeout=90)
        data = json.loads(result.stdout)
        self.assertTrue(data['verified'])
        rows = data['samples']
        self.assertEqual(len(rows), 17)
        self.assertEqual(rows[0]['count'], 2000)
        self.assertEqual({r['action'] for r in rows}, {'none', 'exit', 'exec', 'cow'})
        self.assertTrue(all(r['ns'] > 0 for r in rows))
