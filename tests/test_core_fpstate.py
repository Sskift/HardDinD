"""Byte-exact FP copy elision and native x86 state restoration checks."""
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class FpStateTests(unittest.TestCase):
    def test_complete_image_comparison(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-fp-compare.patch').read_text()
        body = '\n'.join(line[1:] for line in patch.splitlines()
                         if line.startswith('+\tif (!uml_fp_compare') or line.startswith('+\t\tmemcpy('))
        source = '''#include <assert.h>
#include <string.h>
static int uml_fp_compare, copies;
struct registers { unsigned char fp[4096]; };
static void tracked_copy(void *to, const void *from, size_t n) { copies++; memcpy(to, from, n); }
#undef memcpy
#define memcpy tracked_copy
static void restore(struct registers *regs, void *xstate_stub, int xstate_size) {
''' + body + '''
}
int main(void) {
    struct registers regs = {{0}};
    unsigned char image[4096] = {0};
    restore(&regs, image, 832);
    assert(copies == 1);
    uml_fp_compare = 1;
    restore(&regs, image, 832);
    assert(copies == 1);
    regs.fp[831] = 71;
    restore(&regs, image, 832);
    assert(copies == 2 && image[831] == 71 && image[832] == 0);
    regs.fp[2687] = 93;
    restore(&regs, image, 2688);
    assert(copies == 3 && image[2687] == 93 && image[2688] == 0);
    restore(&regs, image, 2688);
    assert(copies == 3);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', str(path / 'check.c'),
                            '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64', 'Linux x86-64 FP ABI')
    def test_static_fpstate_semantics(self):
        compiler = shutil.which('gcc')
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            binary = pathlib.Path(d) / 'probe'
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                            str(config.REPO / 'src/core_fpstate.c'), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary), '4096'], capture_output=True, text=True, timeout=60)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(result.stdout)
            self.assertEqual(row['sse_calls'], 8192)
            self.assertEqual(row['avx_calls'], 8192)
            self.assertTrue(row['signal_upper_edit'] and row['ptrace_upper_edit'] and row['verified'])
