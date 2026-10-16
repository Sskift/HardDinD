import pathlib
import re
import unittest


class SharePatchFormat(unittest.TestCase):
    def test_unified_hunk_lengths(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        paths = sorted((root / 'virtiofs/patches').glob('*.patch'))
        paths += sorted((root / 'kernel/candidates').glob('share-*.patch'))
        pattern = r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@'
        for path in paths:
            remaining = [0, 0]
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                with self.subTest(path=path.name, line=lineno):
                    match = re.match(pattern, line)
                    if match:
                        self.assertEqual(remaining, [0, 0])
                        remaining = [int(value) if value is not None else 1 for value in match.groups()]
                    elif line.startswith('\\ No newline'):
                        continue
                    elif any(remaining):
                        self.assertTrue(line and line[0] in ' +-')
                        if line[0] in ' -': remaining[0] -= 1
                        if line[0] in ' +': remaining[1] -= 1
                        self.assertGreaterEqual(min(remaining), 0)
            self.assertEqual(remaining, [0, 0], path.name)
