"""Sample ownership and native static syscall probe checks."""
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class SyscallProfileTests(unittest.TestCase):
    def test_reply_spin_argument(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-reply-spin.patch').read_text()
        part = patch.split('+++ b/arch/um/os-Linux/start_up.c\n')[1]
        added = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        setup = added.split('__uml_setup(')[0]
        source = '''#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#define __init
#define fatal(message) exit(2)
''' + setup + '''
int main(int argc, char **argv) {
    assert(uml_seccomp_reply_spin == 0);
    int add = 1;
    if (argc == 1) return 0;
    uml_reply_spin_setup(argv[1], &add);
    assert(!add && uml_seccomp_reply_spin == strtoul(argv[1], NULL, 10) + 1);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', str(path / 'check.c'),
                            '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)
            for arg in ['0', '16', '256', '10000', '', '-1', '10001', 'abc', '1x']:
                result = subprocess.run([str(path / 'check'), arg], timeout=10)
                self.assertEqual(result.returncode, 0 if arg.isdecimal() and int(arg) <= 10000 else 2)

    def test_stub_sampling_and_completed_records(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-syscall-profile.patch').read_text()
        part = patch.split('+++ b/arch/um/include/shared/core-profile.h\n')[1].split('\n--- ')[0]
        header = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        start = header.index('static inline __attribute__((always_inline)) unsigned long long uml_sp_clock')
        end = header.index('\n}', start) + 2
        header = header[:start] + 'static unsigned long long tick;\nstatic unsigned long long uml_sp_clock(void) { return ++tick; }' + header[end:]
        source = '#include <assert.h>\n' + header + '''
int main(void) {
    struct uml_sp_stub p = {0};
    for (int i = 0; i < 1024; i++) uml_sp_enter(&p, 1);
    assert(!p.sequence && !p.active && !tick);
    p.enabled = 1;
    for (int i = 1; i <= 1024; i++) {
        uml_sp_enter(&p, 1);
        assert(p.active == !(i & 255));
        if (i == 257 || i == 513 || i == 769) {
            assert(p.valid == 2 && p.gap == 1);
            p.valid = 0;
        }
        if (p.active) {
            p.release = uml_sp_clock();
            uml_sp_replied(&p);
            assert(p.reply == 1 && p.valid == 1);
            p.valid = 0;
            p.resume = uml_sp_clock();
            p.active = 0;
        }
    }
    /* A different intervening signal must not become a syscall return sample. */
    uml_sp_enter(&p, 0);
    assert(!p.active && !p.resume && !p.valid);
    uml_sp_enter(&p, 1);
    assert(!p.valid);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', str(path / 'check.c'),
                            '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64', 'Linux x86-64 signal ABI')
    def test_static_probe_modes_and_signal_verification(self):
        compiler = shutil.which('gcc')
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            binary = pathlib.Path(d) / 'probe'
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                            str(config.REPO / 'src/core_syscalls.c'), '-o', str(binary)], check=True)
            for mode, expected in [('all', [1, 2, 3, 4, 1, 1, 1, 1]), ('threads', [1, 2, 3, 4]),
                                   ('signals', [1]), ('filters', [1, 1]), ('ptrace', [1])]:
                r = subprocess.run([str(binary), mode, '256'], capture_output=True, text=True, timeout=60, check=True)
                rows = json.loads(r.stdout)['samples']
                self.assertEqual([r['threads'] for r in rows], expected)
                for row in rows:
                    self.assertTrue(row['verified'])
                    self.assertEqual(row['operations'], row['threads'] * 256)
                    self.assertGreater(row['ns_per_op'], 0)
