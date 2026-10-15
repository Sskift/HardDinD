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
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                            str(config.REPO / 'src/core_faults.c'), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary), 'semantics'], cwd=d, capture_output=True,
                                    text=True, timeout=60, check=True)
            self.assertIn('CORE_SEMANTICS_END status=0', result.stdout)
            self.assertIn('CORE_CHECK parallel_syscall_handshake pass', result.stdout)
            self.assertTrue(json.loads(result.stdout.splitlines()[-1])['verified'])
            for mode in ('anon', 'file', 'cow', 'small', 'fork', 'exec', 'mmap', 'mprotect', 'getpid', 'getpid4'):
                with self.subTest(mode=mode):
                    result = subprocess.run([str(binary), mode, '8'], cwd=d, capture_output=True,
                                            text=True, timeout=30, check=True)
                    sample = json.loads(result.stdout)
                    self.assertEqual(sample['schema'], 'hd.core/1')
                    self.assertEqual(sample['operations'], 32 if mode == 'getpid4' else 8)
                    self.assertEqual(sample['mode'], mode)
                    self.assertGreater(sample['seconds'], 0)
                    self.assertGreater(sample['ns_per_op'], 0)
                    self.assertGreaterEqual(sample['minor_faults'], 0)
            for group, modes in [('faults', ['anon', 'file', 'cow']),
                                 ('process', ['fork', 'exec', 'mmap', 'mprotect', 'getpid']),
                                 ('syscalls', ['getpid', 'getpid4']),
                                 ('all', ['anon', 'file', 'cow', 'small', 'fork', 'exec', 'mmap', 'mprotect', 'getpid'])]:
                result = subprocess.run([str(binary), group, '2'], cwd=d, capture_output=True,
                                        text=True, timeout=30, check=True)
                samples = json.loads(result.stdout)['samples']
                self.assertEqual([s['mode'] for s in samples], modes)
                self.assertTrue(all(s['seconds'] > 0 for s in samples))
