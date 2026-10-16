"""Persistent namespace observations use the same native and guest probe."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from suites.functional.shared_files import TREE_PROBE


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('gcc'), 'Linux stat ABI')
class TreeProbe(unittest.TestCase):
    def test_namespace_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, binary = root / 'tree.c', root / 'tree'
            source.write_text(TREE_PROBE)
            subprocess.run(['gcc', '-static', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            data = root / 'directory'
            data.mkdir()
            (data / 'file').write_bytes(b'B')
            proc = subprocess.run([str(binary), str(data)], input='stat missing\nread file\nlist .\nhold file\nheld file\nquit\n',
                                  check=True, text=True, capture_output=True, timeout=5)
            ready, missing, read, listing, held, again = map(json.loads, proc.stdout.splitlines())
            self.assertTrue(ready['ready'])
            self.assertEqual((missing['rc'], missing['errno']), (-1, 2))
            self.assertEqual((read['size'], read['byte']), (1, 66))
            self.assertEqual(listing['names'], ['file'])
            self.assertEqual(held, again)
            self.assertEqual(held['byte'], 66)
            (data / 'file').write_bytes(b'B' * 8192)
            proc = subprocess.run([str(binary), str(data)],
                                  input='map file\nmapped 0\nremap 8192\nmapped 4096\nunmap .\nquit\n',
                                  check=True, text=True, capture_output=True, timeout=5)
            ready, mapped, first, remapped, second, unmapped = map(json.loads, proc.stdout.splitlines())
            self.assertTrue(ready['ready'])
            self.assertEqual([mapped['rc'], remapped['rc'], unmapped['rc']], [0, 0, 0])
            self.assertEqual((first['byte'], second['byte']), (66, 66))
