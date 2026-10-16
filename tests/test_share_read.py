import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from suites.functional.shared_files import READ_BUFFER, read_buffer_results


class ReadBuffer(unittest.TestCase):
    def sample(self):
        return '\n'.join([json.dumps({'size': size, 'rep': rep, 'bytes': 256 * 1048576,
                                      'ns': 256000})
                          for size in (65536, 1048576) for rep in range(3)]
                         + ['{"edges":true}'])

    def test_timing_units(self):
        rows = read_buffer_results(self.sample())
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row['us_per_mib'] == 1 for row in rows))

    def test_missing_duplicate_invalid_and_failed_edges(self):
        for text in (self.sample().replace('"rep": 2', '"rep": 1'),
                     self.sample().replace('256000', '0'),
                     self.sample().replace('256000', 'true'),
                     self.sample().replace('"rep": 1', '"rep": true'),
                     self.sample().replace('268435456', '1'),
                     self.sample().replace('{"edges":true}', '{"edges":false}'),
                     self.sample().split('\n', 1)[1]):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_buffer_results(text)

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('gcc'), 'Linux pread fault semantics')
    def test_native_read_guard_readonly_and_cow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, binary = root / 'read.c', root / 'read'
            source.write_text(READ_BUFFER)
            subprocess.run(['gcc', '-static', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            run = subprocess.run([str(binary), str(root / 'data')], check=True, capture_output=True,
                                 text=True, timeout=20)
            self.assertEqual(len(read_buffer_results(run.stdout)), 6)
            self.assertFalse((root / 'data').exists())
