"""Keep optional profiler and FP changes composable on their shared base."""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


def file_patch(text, name):
    start = text.index('--- a/' + name + '\n')
    end = text.find('\n--- ', start + 1)
    return text[start:end + 1] if end >= 0 else text[start:]


def before_lines(patch):
    lines, at = {}, None
    for line in patch.splitlines(keepends=True):
        match = re.match(r'@@ -(\d+)(?:,\d+)? \+', line)
        if match:
            at = int(match[1])
        elif at is not None and line.startswith((' ', '-')):
            lines[at] = line[1:]
            at += 1
    return lines


class CoreCompositionTests(unittest.TestCase):
    def test_profile_then_lazy_fp(self):
        patch = shutil.which('patch')
        if not patch:
            self.skipTest('patch unavailable')
        pairs = [('arch/um/kernel/process.c', 'core-syscall-profile'),
                 ('arch/um/kernel/skas/syscall.c', 'core-syscall-profile'),
                 ('arch/um/os-Linux/skas/process.c', 'fault-rendezvous-counters')]
        # Each pair is written against the same series-applied file contents.
        for name, first in pairs:
            candidates = [config.REPO / 'kernel/candidates' / (p + '.patch')
                          for p in (first, 'core-fp-lazy')]
            parts = [file_patch(p.read_text(), name) for p in candidates]
            known = {}
            for part in parts:
                for number, line in before_lines(part).items():
                    self.assertEqual(known.get(number, line), line, (name, number))
                    known[number] = line
            with tempfile.TemporaryDirectory(prefix='.core-compose-', dir=config.REPO) as directory:
                root = pathlib.Path(directory)
                source = root / name
                source.parent.mkdir(parents=True)
                source.write_text(''.join(known.get(n, '// unchanged %d\n' % n)
                                          for n in range(1, max(known) + 1)))
                for candidate, part in zip(candidates, parts):
                    result = subprocess.run([patch, '--batch', '--forward', '--fuzz=0', '-p1', '-d', root],
                                            input=part, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, str(candidate) + '\n' + result.stdout + result.stderr)
