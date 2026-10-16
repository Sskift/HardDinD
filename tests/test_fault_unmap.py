"""An unmap cache must stop at intervening commands and failed queues."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class FaultUnmapTests(unittest.TestCase):
    def test_repeated_ranges(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-unmap.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith(('+', ' ')) and not line.startswith('+++'))
        start = added.index('int unmap(struct')
        helper = added[start:added.index('\n}', start) + 2]
        source = '''
#include <assert.h>
struct mm_id { unsigned long unmap_start, unmap_end; int syscall_data_len; };
static int uml_fault_unmap, commands;
static int unmap_work(struct mm_id *id, unsigned long addr, unsigned long len) {
    (void)addr; (void)len; id->unmap_end = 0; commands++; return 0;
}
''' + helper + '''
int main(void) {
    struct mm_id id = {0};
    uml_fault_unmap = 1;
    assert(!unmap(&id, 4096, 8192) && commands == 1);
    assert(!unmap(&id, 4096, 8192) && commands == 1);
    assert(!unmap(&id, 8192, 4096) && commands == 1);
    assert(!unmap(&id, 8192, 8192) && commands == 2);
    assert(!unmap(&id, 4096, 8192) && commands == 3);
    id.unmap_end = 0; /* Every new broker command forgets the range. */
    assert(!unmap(&id, 4096, 8192) && commands == 4);
    id.syscall_data_len = -5;
    assert(!unmap(&id, 4096, 8192) && commands == 5);
    assert(!id.unmap_end);
    id.syscall_data_len = 0;
    assert(!unmap(&id, ~0UL - 4095, 8192) && commands == 6);
    assert(!id.unmap_end); /* Wrapped intervals cannot be remembered. */
    uml_fault_unmap = 0;
    assert(!unmap(&id, 4096, 8192) && commands == 7);
    assert(!unmap(&id, 4096, 8192) && commands == 8);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'unmap.c').write_text(source)
            subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'unmap.c'), '-o', str(root / 'unmap')], check=True)
            subprocess.run([str(root / 'unmap')], check=True, timeout=10)
