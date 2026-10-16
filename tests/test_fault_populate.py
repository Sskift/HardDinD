"""Host population must not change protections or single-page mappings."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class PopulateTests(unittest.TestCase):
    def test_writable_multi_page_only(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-populate.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helper = added[added.index('static unsigned int uml_fault_populate_flags'):]
        helper = helper[:helper.index('\n}') + 2]
        source = '''
#include <assert.h>
#define UM_KERN_PAGE_SIZE 4096UL
#define PROT_WRITE 2
#define MAP_POPULATE 0x8000
static unsigned int uml_fault_populate;
''' + helper + '''
int main(void) {
    for (unsigned enabled = 0; enabled <= 1; enabled++) {
        uml_fault_populate = enabled;
        for (unsigned prot = 0; prot < 8; prot++) {
            assert(!uml_fault_populate_flags(4096, prot));
            assert(!uml_fault_populate_flags(0, prot));
            unsigned flags = uml_fault_populate_flags(32 * 4096, prot);
            assert(flags == ((enabled && (prot & PROT_WRITE)) ? MAP_POPULATE : 0));
        }
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'populate.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'populate.c'), '-o', str(root / 'populate')], check=True)
            subprocess.run([str(root / 'populate')], check=True, timeout=10)
