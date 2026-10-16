"""Exercise demand tracking without a running UML kernel."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class SequenceTests(unittest.TestCase):
    def test_sequential_sparse_repeat_and_wrap(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/patches/0036-um-anon-faultaround.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helper = added[added.index('static bool um_faultaround_sequential'):]
        helper = helper[:helper.index('\n}') + 2]
        source = '''
#include <assert.h>
#include <stdbool.h>
#define PAGE_SIZE 4096UL
#define PAGE_MASK (~(PAGE_SIZE - 1))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, value) ((x) = (value))
''' + helper + '''
int main(void) {
    unsigned long last = ~0UL, other = ~0UL;
    for (unsigned long window = 1; window < 100; window++) {
        unsigned long start = window * 32 * PAGE_SIZE;
        assert(!um_faultaround_sequential(&last, start + 17));
        assert(um_faultaround_sequential(&last, start + PAGE_SIZE + 3));
        /* The next 30 PTEs are prefaulted; they are not actual faults. */
    }
    last = ~0UL;
    for (unsigned long i = 0; i < 100; i++)
        assert(!um_faultaround_sequential(&last, i * 32 * PAGE_SIZE));
    assert(!um_faultaround_sequential(&last, last + 17));
    assert(!um_faultaround_sequential(&last, last - PAGE_SIZE));
    assert(!um_faultaround_sequential(&other, last + PAGE_SIZE));
    assert(um_faultaround_sequential(&last, last + PAGE_SIZE));
    last = ~0UL & PAGE_MASK;
    assert(!um_faultaround_sequential(&last, 0));
    assert(um_faultaround_sequential(&last, PAGE_SIZE));
}
'''
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)
            (path / 'sequence.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(path / 'sequence.c'), '-o', str(path / 'sequence')], check=True)
            subprocess.run([str(path / 'sequence')], check=True, timeout=10)
