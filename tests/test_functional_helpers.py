import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


BINARIES = os.environ.get('HD_FUNCTIONAL_HELPERS')


@unittest.skipUnless(sys.platform == 'linux' and BINARIES,
                     'requires Linux static helpers (HD_FUNCTIONAL_HELPERS)')
class NativeHelperTests(unittest.TestCase):
    def run_helper(self, name, *args, input=None):
        result = subprocess.run([str(pathlib.Path(BINARIES) / name), *args], input=input,
                                text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_static_elf(self):
        for name in ('corebench', 'dispatchbench', 'mapping_semantics', 'thread_semantics', 'share_probe', 'probe'):
            with self.subTest(name=name):
                result = subprocess.run(['readelf', '-l', str(pathlib.Path(BINARIES) / name)],
                                        capture_output=True, text=True, timeout=10, check=True)
                self.assertNotIn('INTERP', result.stdout)

    def test_compute_checksums_and_signals(self):
        for mode in ('threads', 'processes', 'signals', 'exec', 'syscall'):
            with self.subTest(mode=mode):
                self.assertTrue(json.loads(self.run_helper('corebench', mode, '4'))['verified'])

    def test_dispatch(self):
        for mode in ('identity', 'fdio', 'mapping', 'thread_syscalls', 'isolation', 'parallel_exec'):
            with self.subTest(mode=mode):
                self.assertTrue(json.loads(self.run_helper('dispatchbench', mode))['verified'])

    def test_memory_and_thread_semantics(self):
        self.assertIn('MAPPING_SEMANTICS_END status=0', self.run_helper('mapping_semantics'))
        self.assertIn('THREAD_SEMANTICS_END status=0', self.run_helper('thread_semantics'))

    def test_share_protocol(self):
        with tempfile.TemporaryDirectory(dir=BINARIES) as root:
            path = pathlib.Path(root) / 'file'
            path.write_bytes(b'A' * 8192)
            out = self.run_helper('share_probe', str(path), input='open\nread\nlock\nunlock\nposix-wlock 0 64\nposix-unlock 0 64\nclose\nquit\n')
            rows = [json.loads(line) for line in out.splitlines()]
            self.assertTrue(rows[0]['ready'])
            self.assertTrue(all(row['rc'] == 0 for row in rows[1:]))
            self.assertEqual(rows[2]['byte'], 65)
