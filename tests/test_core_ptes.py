"""Execute the candidate's real set_ptes body with small page-table shims."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class SetPtesTests(unittest.TestCase):
    def test_consecutive_pfns_and_sync_range(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/patches/0033-um-set-ptes-pfn-stride.patch').read_text()
        after = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith((' ', '+')) and not line.startswith('+++'))
        body = after[after.index('static inline void set_ptes'):]
        body = body[:body.index('\n}') + 2]
        source = '''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#define PAGE_SIZE 4096UL
#define PFN_PTE_SHIFT 12
typedef uint64_t pte_t;
#define pte_val(x) (x)
#define __pte(x) (x)
struct mm_struct { unsigned long start, end; int syncs; };
static void set_pte(pte_t *p, pte_t value) { *p = value | 2; }
static void um_tlb_mark_sync(struct mm_struct *m, unsigned long a, unsigned long b)
{ m->start = a; m->end = b; m->syncs++; }
''' + body + '''
int main(void) {
    for (int count = 1; count <= 512; count++) {
        pte_t entries[514] = {0};
        struct mm_struct m = {0};
        const pte_t first = (0x12345UL << PFN_PTE_SHIFT) | 0x145;
        set_ptes(&m, 0x200000, entries + 1, first, count);
        for (int i = 0; i < count; i++)
            if (entries[i + 1] != ((first + i * PAGE_SIZE) | 2)) return count;
        assert(entries[0] == 0 && entries[count + 1] == 0);
        assert(m.start == 0x200000 && m.end == 0x200000 + count * PAGE_SIZE);
        assert(m.syncs == 1);
    }
}
'''
        # All checks run through hd; keep their temporary files in this checkout.
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(path / 'check.c'), '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)
            # Verify that the test distinguishes the original three-page bug.
            original = source.replace('(1UL << PFN_PTE_SHIFT)', '(nr << PFN_PTE_SHIFT)')
            self.assertNotEqual(original, source)
            (path / 'check.c').write_text(original)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(path / 'check.c'), '-o', str(path / 'before')], check=True)
            result = subprocess.run([str(path / 'before')], timeout=10)
            self.assertEqual(result.returncode, 3)
