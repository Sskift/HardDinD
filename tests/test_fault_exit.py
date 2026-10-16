"""Whole-address-space invalidation is restricted to dead non-GPU user mms."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class FaultExitTests(unittest.TestCase):
    def test_eligibility(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/patches/0045-um-fault-exit.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        start = added.index('static bool uml_fault_dead_mm(')
        helper = added[start:added.index('\n}', start) + 2]
        source = '''
#include <assert.h>
#include <stdbool.h>
#define atomic_read(p) (*(p))
struct mm_struct {
    int mm_users;
    struct { struct { unsigned long stack; int pid; } gpu_id, id; } context;
};
static struct mm_struct init_mm;
static int uml_fault_exit, parallel_userspace;
''' + helper + '''
int main(void) {
    for (int on = 0; on < 2; on++) for (int parallel = 0; parallel < 2; parallel++)
    for (int users = 0; users < 3; users++) for (int gpu = 0; gpu < 2; gpu++)
    for (int stack = 0; stack < 2; stack++) for (int pid = -1; pid < 4; pid++) {
        struct mm_struct mm = {.mm_users = users, .context.gpu_id.stack = gpu,
                               .context.id = {.stack = stack, .pid = pid}};
        uml_fault_exit = on; parallel_userspace = parallel;
        assert(uml_fault_dead_mm(&mm) == (on && parallel && !users && !gpu && stack && pid > 1));
        init_mm = mm;
        assert(!uml_fault_dead_mm(&init_mm));
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'exit.c').write_text(source)
            subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'exit.c'), '-o', str(root / 'exit')], check=True)
            subprocess.run([str(root / 'exit')], check=True, timeout=10)
