"""Compile everywhere; check native per-page residency on Linux."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class ResidencyTests(unittest.TestCase):
    def test_build(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / 'residency'
            flags = ['-static'] if sys.platform == 'linux' else []
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(config.REPO / 'src/fault_residency.c'), '-o', str(binary)], check=True)

    @unittest.skipUnless(sys.platform == 'linux', 'MADV_DONTNEED zero-fill semantics require Linux')
    def test_native_residency_and_cow(self):
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / 'residency'
            subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                            str(config.REPO / 'src/fault_residency.c'), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=30)
            row = json.loads(result.stdout)
            self.assertTrue(row['verified'])
            self.assertEqual([row[k] for k in ('sparse_pages', 'dense_first_pages',
                                             'dense_second_pages', 'fork_child_pages')], [2, 1, 2, 2])
