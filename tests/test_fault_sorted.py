"""Window ownership, PFN ordering and unused-page cleanup of the actual helpers."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class FaultSortedTests(unittest.TestCase):
    def test_pool_scope_order_and_cleanup(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-sorted-alloc.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helpers = []
        for prefix in ('static bool uml_fault_pool_matches',
                       'static int uml_fault_pfn_compare', 'void uml_fault_pool_end'):
            start = added.rindex(prefix)
            end = added.index('\n}', start) + 2
            helpers.append(added[start:end])
        source = '''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <limits.h>
struct task_struct { int unused; };
struct vm_area_struct { int unused; };
struct page { unsigned long pfn; unsigned freed; };
struct uml_fault_pool {
    unsigned link;
    struct task_struct *owner;
    struct vm_area_struct *vma;
    struct page **pages;
    unsigned long start, end;
    unsigned count, used;
};
#define page_to_pfn(page) ((page)->pfn)
#define spin_lock_irqsave(lock, flags) ((flags) = 0)
#define spin_unlock_irqrestore(lock, flags) ((void)(flags))
static unsigned releases, arrays;
static void list_del(unsigned *link) { assert(*link == 1); *link = 0; }
static void __free_page(struct page *page) { assert(!page->freed++); releases++; }
static void kfree(void *ptr) { assert(ptr); arrays++; }
''' + '\n'.join(helpers) + '''
int main(void) {
    struct task_struct a = {0}, b = {0};
    struct vm_area_struct x = {0}, y = {0};
    struct page pages[] = {{ULONG_MAX, 0}, {7, 0}, {0, 0}, {9, 0}};
    struct page *array[] = {pages, pages + 1, pages + 2, pages + 3};
    qsort(array, 4, sizeof(*array), uml_fault_pfn_compare);
    for (unsigned i = 1; i < 4; i++) assert(array[i-1]->pfn < array[i]->pfn);
    struct uml_fault_pool pool = {1, &a, &x, array, 4096, 4096 * 513, 4, 0};
    assert(!uml_fault_pool_matches(&pool, &x, 4095, &a));
    assert(!uml_fault_pool_matches(&pool, &x, pool.end, &a));
    assert(!uml_fault_pool_matches(&pool, &x, ULONG_MAX, &a));
    for (unsigned i = 0; i < 512; i++) {
        unsigned long address = 4096UL * (i + 1);
        assert(uml_fault_pool_matches(&pool, &x, address, &a));
        assert(!uml_fault_pool_matches(&pool, &x, address, &b));
        assert(!uml_fault_pool_matches(&pool, &y, address, &a));
    }
    pool.used = pool.count;
    assert(!uml_fault_pool_matches(&pool, &x, 4096, &a));
    pool.used = 2;
    uml_fault_pool_end(&pool);
    assert(releases == 2 && arrays == 1 && !pool.link);
    assert(!array[0]->freed && !array[1]->freed && array[2]->freed && array[3]->freed);
    pool.pages = NULL;
    uml_fault_pool_end(&pool);
    assert(releases == 2 && arrays == 1);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'sorted.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'sorted.c'), '-o', str(root / 'sorted')], check=True)
            subprocess.run([str(root / 'sorted')], check=True, timeout=10)
