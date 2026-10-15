"""Check the shared counter helpers independently of either kernel C environment."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class CounterTests(unittest.TestCase):
    def test_disabled_and_concurrent_updates(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-rendezvous-counters.patch').read_text()
        part = patch.split('+++ b/arch/um/include/shared/faultstat.h\n', 1)[1]
        part = part.split('\n--- ', 1)[0]
        header = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        source = header + '''
#include <assert.h>
#include <pthread.h>
int uml_faultstat_enabled;
unsigned long long uml_fs_values[UML_FS_COUNT];
static void *worker(void *unused) {
    (void)unused;
    for (int i = 0; i < 10000; i++) {
        uml_fs_inc(UML_FS_trap);
        uml_fs_add(UML_FS_map_pages, 32);
    }
    return 0;
}
int main(void) {
    pthread_t ids[4];
    worker(0);
    assert(uml_fs_values[UML_FS_trap] == 0);
    assert(uml_fs_values[UML_FS_map_pages] == 0);
    uml_faultstat_enabled = 1;
    for (int i = 0; i < 4; i++) assert(!pthread_create(ids + i, 0, worker, 0));
    for (int i = 0; i < 4; i++) assert(!pthread_join(ids[i], 0));
    assert(uml_fs_values[UML_FS_trap] == 40000);
    assert(uml_fs_values[UML_FS_map_pages] == 1280000);
    uml_faultstat_enabled = 0;
    worker(0);
    assert(uml_fs_values[UML_FS_trap] == 40000);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
                            str(path / 'check.c'), '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)
