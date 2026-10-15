"""Native Linux smoke coverage; guest execution belongs to the functional suite."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


@unittest.skipUnless(sys.platform == 'linux', 'static checker requires Linux')
class CoreFaultsTests(unittest.TestCase):
    def test_static_semantics_and_measurement_modes(self):
        compiler = shutil.which('gcc')
        self.assertIsNotNone(compiler, 'gcc is required on the Linux build host')
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            directory = pathlib.Path(d)
            binary = directory / 'core-faults'
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                            str(config.REPO / 'src/core_faults.c'), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary), 'semantics'], cwd=d, capture_output=True,
                                    text=True, timeout=60, check=True)
            self.assertIn('CORE_SEMANTICS_END status=0', result.stdout)
            for mode in ('anon', 'file', 'cow', 'fork', 'exec', 'mmap', 'mprotect', 'getpid'):
                with self.subTest(mode=mode):
                    result = subprocess.run([str(binary), mode, '8'], cwd=d, capture_output=True,
                                            text=True, timeout=30, check=True)
                    sample = json.loads(result.stdout)
                    self.assertEqual(sample['schema'], 'hd.core/1')
                    self.assertEqual(sample['operations'], 8)
                    self.assertEqual(sample['mode'], mode)
                    self.assertGreater(sample['seconds'], 0)
                    self.assertGreater(sample['ns_per_op'], 0)
                    self.assertGreaterEqual(sample['minor_faults'], 0)
