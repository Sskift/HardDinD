"""Exercise task ownership, partial allocation, fallback and cleanup in the helpers."""
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
        for prefix in ('static bool uml_fault_pool_matches', 'static int uml_fault_pfn_compare',
                       'void uml_fault_pool_begin', 'static struct folio *uml_fault_pool_take',
                       'struct folio *uml_vma_alloc_zeroed_movable_folio', 'void uml_fault_pool_end'):
            start = added.rindex(prefix)
            end = added.index('\n}', start) + 2
            helpers.append(added[start:end])
        struct_start = added.index('struct uml_fault_pool {')
        pool_struct = added[struct_start:added.index('\n};', struct_start) + 3]
        source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#define PAGE_SIZE 4096UL
#define PMD_MASK (~(512 * PAGE_SIZE - 1))
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define READ_ONCE(x) (x)
#define GFP_HIGHUSER_MOVABLE 1U
#define __GFP_RECLAIM 2U
#define __GFP_NOWARN 4U
#define __GFP_NOMEMALLOC 8U
#define GFP_NOWAIT 16U
#define CONFIG_NUMA numa
#define IS_ENABLED(x) (x)
typedef unsigned gfp_t;
typedef unsigned pte_t;
typedef unsigned pmd_t;
typedef unsigned spinlock_t;
struct uml_fault_pool;
struct task_struct { struct { struct uml_fault_pool *fault_pool; } thread; };
struct memory { pmd_t present; pte_t ptes[512]; };
struct vm_area_struct { struct memory *vm_mm; };
struct page { unsigned long pfn; unsigned freed, value; };
struct folio { struct page page; };
''' + pool_struct + r'''
static struct task_struct first, second, *current = &first;
static struct folio pages[4096];
static unsigned uml_fault_sorted, numa, releases, arrays, heap_allocs;
static unsigned used_pages, alloc_limit = 512, fail_array, zero = 1, fallback, cleared;
static spinlock_t pte_lock;
#define page_to_pfn(page) ((page)->pfn)
#define page_folio(page) ((struct folio *)(page))
static pmd_t *pmd_off(struct memory *mm, unsigned long addr) { (void)addr; return &mm->present; }
static bool pmd_present(pmd_t pmd) { return pmd != 0; }
static pte_t *pte_offset_map_lock(struct memory *mm, pmd_t *pmd, unsigned long addr, spinlock_t **ptl) {
    assert(*pmd); *ptl = &pte_lock; return mm->ptes + addr / PAGE_SIZE % 512;
}
static bool pte_none(pte_t pte) { return !pte; }
static void pte_unmap_unlock(pte_t *pte, spinlock_t *ptl) { assert(pte && ptl == &pte_lock); }
static void *kcalloc(unsigned count, size_t size, gfp_t flags) {
    assert(flags == (GFP_NOWAIT | __GFP_NOWARN)); heap_allocs++;
    return fail_array ? NULL : calloc(count, size);
}
static struct page *allocate_page(void) {
    assert(used_pages < ARRAY_SIZE(pages));
    struct page *p = &pages[used_pages++].page;
    *p = (struct page){4096 - used_pages, 0, 0x99}; return p;
}
static unsigned alloc_pages_bulk(gfp_t flags, unsigned count, struct page **out) {
    assert(flags == (GFP_HIGHUSER_MOVABLE | __GFP_NOWARN | __GFP_NOMEMALLOC));
    unsigned n = count < alloc_limit ? count : alloc_limit;
    for (unsigned i = 0; i < count; i++) assert(!out[i]);
    for (unsigned i = 0; i < n; i++) out[i] = allocate_page();
    return n;
}
static void sort(void *base, size_t count, size_t size,
                 int (*compare)(const void *, const void *), void *unused) {
    assert(!unused); qsort(base, count, size, compare);
}
static void __free_page(struct page *p) {
    assert(!current->thread.fault_pool); /* Detach before any free. */
    assert(!p->freed++); releases++;
}
static void kfree(void *ptr) { assert(ptr); free(ptr); arrays++; }
static struct folio *vma_alloc_folio(gfp_t gfp, unsigned order, struct vm_area_struct *vma, unsigned long addr) {
    assert(gfp == GFP_HIGHUSER_MOVABLE && !order && vma && !(addr % PAGE_SIZE));
    fallback++; return page_folio(allocate_page());
}
static bool user_alloc_needs_zeroing(void) { return zero; }
static void clear_user_highpage(struct page *p, unsigned long addr) { assert(!(addr % PAGE_SIZE)); p->value = 0; cleared++; }
''' + '\n'.join(helpers) + r'''
int main(void) {
    struct memory mm = {.present = 1};
    struct vm_area_struct vma = {&mm}, other = {&mm};
    struct uml_fault_pool a, b, nested;
    uml_fault_pool_begin(&a, &vma, PAGE_SIZE, 33 * PAGE_SIZE);
    assert(!a.pages && !current->thread.fault_pool); /* Disabled. */
    uml_fault_sorted = 1;
    uml_fault_pool_begin(&a, &vma, PAGE_SIZE, 33 * PAGE_SIZE);
    assert(a.pages == a.local_pages && a.count == 32 && !heap_allocs);
    for (unsigned i = 1; i < a.count; i++) assert(page_to_pfn(a.pages[i-1]) < page_to_pfn(a.pages[i]));
    assert(!uml_fault_pool_take(&other, PAGE_SIZE));
    assert(!uml_fault_pool_take(&vma, 0));
    assert(!uml_fault_pool_take(&vma, a.end));
    assert(!uml_fault_pool_take(&vma, ULONG_MAX));
    uml_fault_pool_begin(&nested, &vma, PAGE_SIZE, 33 * PAGE_SIZE);
    assert(!nested.pages && current->thread.fault_pool == &a);
    uml_fault_pool_end(&nested);
    assert(current->thread.fault_pool == &a);
    struct folio *taken = uml_vma_alloc_zeroed_movable_folio(&vma, PAGE_SIZE);
    assert(taken == page_folio(a.pages[0]) && !taken->page.value && cleared == 1 && !fallback);
    current = &second;
    assert(!uml_fault_pool_take(&vma, PAGE_SIZE));
    uml_fault_pool_begin(&b, &vma, PAGE_SIZE, 33 * PAGE_SIZE);
    assert(b.pages && first.thread.fault_pool == &a && second.thread.fault_pool == &b);
    assert(uml_fault_pool_take(&vma, PAGE_SIZE) == page_folio(b.pages[0]));
    uml_fault_pool_end(&b);
    assert(releases == 31 && !arrays && first.thread.fault_pool == &a);
    current = &first;
    uml_fault_pool_end(&a);
    assert(releases == 62 && !arrays && !first.thread.fault_pool && !a.pages);
    assert(!taken->page.freed);
    uml_fault_pool_end(&a); /* Idempotent cleanup. */
    assert(releases == 62);
    for (unsigned limit = 0; limit < 3; limit++) {
        alloc_limit = limit;
        uml_fault_pool_begin(&a, &vma, 0, 64 * PAGE_SIZE);
        assert(a.count == limit && a.pages != a.local_pages && heap_allocs == limit + 1);
        for (unsigned i = 0; i < limit; i++) assert(uml_fault_pool_take(&vma, i * PAGE_SIZE));
        assert(!uml_fault_pool_take(&vma, 0));
        uml_fault_pool_end(&a);
        assert(releases == 62 && arrays == limit + 1 && !current->thread.fault_pool);
    }
    alloc_limit = 7;
    uml_fault_pool_begin(&a, &vma, 0, 64 * PAGE_SIZE);
    assert(a.count == 7); /* Partial bulk allocation and an early fault exit. */
    assert(uml_fault_pool_take(&vma, 0));
    uml_fault_pool_end(&a);
    assert(releases == 68 && arrays == 4);
    fail_array = 1;
    uml_fault_pool_begin(&a, &vma, 0, 64 * PAGE_SIZE);
    assert(!a.pages && !current->thread.fault_pool);
    uml_fault_pool_end(&a);
    numa = 1;
    uml_fault_pool_begin(&a, &vma, 0, 32 * PAGE_SIZE);
    assert(!a.pages && !current->thread.fault_pool);
    taken = uml_vma_alloc_zeroed_movable_folio(&vma, 0);
    assert(!taken->page.value && fallback == 1 && cleared == 2);
    zero = 0;
    taken = uml_vma_alloc_zeroed_movable_folio(&vma, 0);
    assert(taken->page.value == 0x99 && fallback == 2 && cleared == 2);
    numa = 0;
    mm.present = 0;
    uml_fault_pool_begin(&a, &vma, 0, 32 * PAGE_SIZE);
    assert(!a.pages);
    mm.present = 1;
    for (unsigned i = 0; i < 32; i++) mm.ptes[i] = 1;
    uml_fault_pool_begin(&a, &vma, 0, 32 * PAGE_SIZE);
    assert(!a.pages); /* Existing zero/COW/swap PTEs are not a pool budget. */
    uml_fault_pool_begin(&a, &vma, 0, 513 * PAGE_SIZE);
    assert(!a.pages);
    uml_fault_pool_begin(&a, &vma, 511 * PAGE_SIZE, 513 * PAGE_SIZE);
    assert(!a.pages);
    assert(!current->thread.fault_pool);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'sorted.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'sorted.c'), '-o', str(root / 'sorted')], check=True)
            subprocess.run([str(root / 'sorted')], check=True, timeout=10)
