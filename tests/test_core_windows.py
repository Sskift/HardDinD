"""Exercise the candidate's window calculation across VMA and PMD boundaries."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class WindowTests(unittest.TestCase):
    def test_clipped_windows(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/patches/0037-um-faultaround-edges.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        body = added[added.index('static bool um_faultaround_window'):]
        body = body[:body.index('\n}') + 2]
        source = '''
#include <assert.h>
#include <stdbool.h>
#define PAGE_SIZE 4096UL
#define PMD_MASK (~(512 * PAGE_SIZE - 1))
#define ALIGN_DOWN(a, n) ((a) & ~((n) - 1))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define max(a, b) ((a) > (b) ? (a) : (b))
''' + body + '''
int main(void) {
    unsigned long from, to;
    for (unsigned int order = 1; order <= 5; order++) {
        unsigned long size = PAGE_SIZE << order;
        for (unsigned long first = 490; first < 530; first++) {
            for (unsigned long count = 1; count <= 40; count++) {
                unsigned long start = first * PAGE_SIZE, end = start + count * PAGE_SIZE;
                for (unsigned long page = first; page < first + count; page++) {
                    unsigned long address = page * PAGE_SIZE + 17;
                    unsigned long lo = ALIGN_DOWN(address, size), hi = lo + size;
                    bool full = lo >= start && hi <= end;
                    assert(um_faultaround_window(address, start, end, order, false, &from, &to) == full);
                    if (full) assert(from == lo && to == hi);
                    unsigned long clipped_lo = max(lo, start), clipped_hi = min(hi, end);
                    bool eligible = clipped_hi - clipped_lo >= 2 * PAGE_SIZE;
                    assert(um_faultaround_window(address, start, end, order, true, &from, &to) == eligible);
                    if (!eligible) continue;
                    assert(from == clipped_lo && to == clipped_hi);
                    assert(from <= address && to > address && to - from <= size);
                    assert(from >= start && to <= end && !(from % PAGE_SIZE) && !(to % PAGE_SIZE));
                    assert((from & PMD_MASK) == ((to - PAGE_SIZE) & PMD_MASK));
                }
            }
        }
    }
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(path / 'check.c'), '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)
