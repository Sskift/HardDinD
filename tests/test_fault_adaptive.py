"""Exercise the actual growth and memory-headroom rules without a guest."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class AdaptiveFaultTests(unittest.TestCase):
    def test_growth_reset_cap_and_headroom(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-adaptive.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helpers = []
        for name in ('um_faultaround_next_order', 'um_faultaround_budget_order'):
            helper = added[added.index('static unsigned int ' + name):]
            helpers.append(helper[:helper.index('\n}') + 2])
        source = '''
#include <assert.h>
#include <limits.h>
#define PAGE_SIZE 4096UL
''' + '\n'.join(helpers) + '''
int main(void) {
    unsigned hits = 0, order = 0;
    unsigned long end = 0, address = PAGE_SIZE;
    order = um_faultaround_next_order(address, end, order, 5, 9, &hits);
    assert(order == 5 && hits == 1);
    for (unsigned i = 0; i < 100; i++) {
        unsigned long size = PAGE_SIZE << order;
        end = (address & ~(size - 1)) + size;
        address = end + PAGE_SIZE;
        unsigned previous = order;
        order = um_faultaround_next_order(address, end, order, 5, 9, &hits);
        assert(order >= previous && order <= previous + 1 && order <= 9);
    }
    assert(order == 9);
    order = um_faultaround_next_order(address + PAGE_SIZE, end, order, 5, 9, &hits);
    assert(order == 5 && hits == 1);
    hits = 400; /* Racing histories are advisory, never an unchecked shift. */
    assert(um_faultaround_next_order(8192, 4096, 999, 5, 9, &hits) == 5);
    assert(um_faultaround_next_order(0, ULONG_MAX - PAGE_SIZE + 1, 9, 5, 9, &hits) == 5);
    assert(um_faultaround_next_order(8192, 4096, 5, 5, 3, &hits) == 5);
    for (unsigned long margin = 0; margin < 4096; margin++) {
        unsigned allowed = um_faultaround_budget_order(margin, 9, 5);
        assert(!allowed || (allowed >= 5 && allowed <= 9 && (1UL << allowed) <= margin / 2));
    }
    assert(um_faultaround_budget_order(63, 9, 5) == 0);
    assert(um_faultaround_budget_order(64, 9, 5) == 5);
    assert(um_faultaround_budget_order(1024, 9, 5) == 9);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'adaptive.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'adaptive.c'), '-o', str(root / 'adaptive')], check=True)
            subprocess.run([str(root / 'adaptive')], check=True, timeout=10)
