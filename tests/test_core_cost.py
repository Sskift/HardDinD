"""Fixed-work validation and syscall accounting boundaries."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class CoreCostTests(unittest.TestCase):
    def test_fixed_work(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        with tempfile.TemporaryDirectory(prefix='.core-cost-', dir=config.REPO) as directory:
            binary = pathlib.Path(directory) / 'cost'
            subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror',
                            *(['-static'] if sys.platform == 'linux' else []),
                            config.REPO / 'src/core_cost.c', '-o', binary], check=True)
            for mode, count in [('forkexec', 8), ('pipe', 128)]:
                result = subprocess.run([binary, mode, str(count)], capture_output=True,
                                        text=True, check=True, timeout=30)
                row = json.loads(result.stdout)
                self.assertEqual(row['schema'], 'hd.core-cost/1')
                self.assertEqual(row['operations'], count)
                self.assertTrue(row['verified'])
                self.assertGreater(row['seconds'], 0)
                self.assertGreaterEqual(row['cpu_seconds'], 0)
            result = subprocess.run([binary, 'group', '8'], capture_output=True,
                                    text=True, check=True, timeout=30)
            group = json.loads(result.stdout)
            self.assertTrue(group['verified'])
            self.assertEqual([(s['mode'], s['operations']) for s in group['samples']],
                             [('snapshot', 1), ('forkexec', 8), ('pipe', 512)])
            self.assertTrue(all(s['verified'] for s in group['samples']))
            result = subprocess.run([binary, 'forkexec', '1', '/missing/core-program'],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('"verified":true', result.stdout)

    def test_syscall_bounds_and_disabled_path(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-syscall-profile.patch').read_text()
        part = patch.split('+++ b/arch/um/kernel/core-profile.c\n', 1)[1].split('\n--- ')[0]
        source = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        body = source.split('void uml_sp_syscall(', 1)[1].split('\n}', 1)[0]
        count = source.split('void uml_sp_count(', 1)[1].split('\n}', 1)[0]
        harness = '''#include <assert.h>
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define UML_SP_syscall 0
#define raw_smp_processor_id() 0
static int uml_sp_enabled;
static unsigned long long syscall_counts[512];
static struct { unsigned long long count[1]; } stats[1];
#define total (stats[0].count[0])
void uml_sp_count(''' + count + '''
}
void uml_sp_syscall(''' + body + '''
}
int main(void) {
    uml_sp_count(0, 1); assert(!total);
    uml_sp_syscall(39); assert(!total && !syscall_counts[39]);
    uml_sp_enabled = 1;
    uml_sp_syscall(0); uml_sp_syscall(39); uml_sp_syscall(511);
    uml_sp_syscall(512); uml_sp_syscall(~0UL);
    assert(total == 5 && syscall_counts[0] == 1 && syscall_counts[39] == 1 && syscall_counts[511] == 1);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-cost-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(harness)
            subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)
