"""Compile the composed candidate's real allocation and cleanup helpers."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class FaultContiguousTests(unittest.TestCase):
    def test_exact_chunks_split_refs_partial_bulk_and_cleanup(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        original = (config.REPO / 'kernel/candidates/fault-sorted-alloc.patch').read_text()
        section = original.split('+++ b/arch/um/kernel/fault-sorted.c\n', 1)[1]
        source = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+')) + '\n'
        header = original.split('+++ b/arch/um/include/shared/fault-sorted.h\n', 1)[1].split('--- /dev/null', 1)[0]
        header = '\n'.join(line[1:] for line in header.splitlines() if line.startswith('+'))
        start = header.index('struct uml_fault_pool {')
        pool = header[start:header.index('\n};', start) + 3]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / 'arch/um/kernel/fault-sorted.c'
            target.parent.mkdir(parents=True)
            target.write_text(source)
            subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(root)],
                           input=(config.REPO / 'kernel/candidates/fault-contiguous.patch').read_bytes(),
                           check=True, capture_output=True)
            composed = target.read_text()
            helpers = []
            for prefix in ('static unsigned int uml_fault_pool_allocate', 'void uml_fault_pool_end'):
                start = composed.index(prefix)
                helpers.append(composed[start:composed.index('\n}', start) + 2])
            program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#define __GFP_RECLAIM 3U
#define __GFP_COMP 4U
#define __GFP_HIGH 8U
#define __GFP_MEMALLOC 16U
#define __GFP_NOWARN 32U
#define __GFP_NOMEMALLOC 64U
#define OTHER_POLICY 128U
#define CONFIG_NUMA numa
#define IS_ENABLED(x) (x)
typedef unsigned gfp_t;
struct uml_fault_pool;
struct task_struct { struct { struct uml_fault_pool *fault_pool; } thread; };
struct vm_area_struct { unsigned unused; };
struct page { unsigned refs, compound, order, freed; };
''' + pool + r'''
static unsigned uml_fault_contiguous, numa, calls, splits, high_pages, fail_at;
static unsigned bulk_limit, bulk_calls, frees, next_page, requested;
static unsigned orders[16], allocation_sizes[16];
static struct page storage[64];
static struct page *dest[64];
static struct task_struct task, *current = &task;
static gfp_t supplied_gfp;
static unsigned ilog2(unsigned n) { assert(n); return 31U - (unsigned)__builtin_clz(n); }
static struct page *alloc_pages(gfp_t gfp, unsigned order) {
    assert(uml_fault_contiguous && !numa && order > 0 && order <= 5);
    assert(!(gfp & (__GFP_RECLAIM | __GFP_COMP | __GFP_HIGH | __GFP_MEMALLOC)));
    assert((gfp & (__GFP_NOWARN | __GFP_NOMEMALLOC | OTHER_POLICY)) ==
           (__GFP_NOWARN | __GFP_NOMEMALLOC | OTHER_POLICY));
    assert(order == ilog2(requested - high_pages));
    orders[calls++] = order;
    if (fail_at && calls == fail_at) return NULL;
    unsigned n = 1U << order;
    assert(high_pages + n <= requested && next_page + n <= 64);
    struct page *p = storage + next_page;
    for (unsigned i = 0; i < n; i++) p[i] = (struct page){i == 0, 0, order, 0};
    next_page += n; high_pages += n;
    allocation_sizes[splits] = n;
    return p;
}
/* Linux 7.2.10 split_page's checked contract: non-compound refcounted
 * head, individually refcounted tails, each page subsequently freed alone. */
static void split_page(struct page *p, unsigned order) {
    unsigned n = 1U << order;
    assert(allocation_sizes[splits++] == n && !p->compound && p->refs == 1);
    for (unsigned i = 0; i < requested; i++)
        if (dest[i]) assert(dest[i]->refs == 1 && !dest[i]->compound);
    for (unsigned i = 1; i < n; i++) {
        assert(!p[i].refs && !p[i].compound); p[i].refs = 1;
    }
    for (unsigned i = 0; i < n; i++) p[i].order = 0;
}
static unsigned alloc_pages_bulk(gfp_t gfp, unsigned count, struct page **pages) {
    bulk_calls++;
    assert(gfp == supplied_gfp && pages == dest + high_pages);
    assert(count == requested - high_pages);
    for (unsigned i = 0; i < high_pages; i++) assert(dest[i] && dest[i]->refs == 1);
    for (unsigned i = 0; i < count; i++) assert(!pages[i]);
    unsigned n = count < bulk_limit ? count : bulk_limit;
    assert(next_page + n <= 64);
    for (unsigned i = 0; i < n; i++) {
        struct page *p = storage + next_page++;
        *p = (struct page){1, 0, 0, 0}; pages[i] = p;
    }
    return n;
}
static void __free_page(struct page *p) {
    assert(!current->thread.fault_pool && p->refs == 1 && !p->compound && !p->order && !p->freed);
    p->refs--; p->freed++; frees++;
}
static void kfree(void *p) { (void)p; abort(); }
''' + '\n'.join(helpers) + r'''
static void reset(unsigned count) {
    memset(storage, 0, sizeof(storage)); memset(dest, 0, sizeof(dest));
    memset(orders, 0, sizeof(orders)); memset(allocation_sizes, 0, sizeof(allocation_sizes));
    calls = splits = high_pages = bulk_calls = frees = next_page = fail_at = numa = 0;
    bulk_limit = 64; requested = count; uml_fault_contiguous = 1;
    supplied_gfp = OTHER_POLICY | __GFP_NOWARN | __GFP_NOMEMALLOC;
    assert(!current->thread.fault_pool);
}
static void verify_and_release(unsigned returned) {
    assert(returned == next_page && returned <= requested);
    for (unsigned i = 0; i < returned; i++) {
        assert(dest[i] && dest[i]->refs == 1 && !dest[i]->compound && !dest[i]->order);
        for (unsigned j = 0; j < i; j++) assert(dest[i] != dest[j]);
    }
    for (unsigned i = returned; i < 64; i++) assert(!dest[i]);
    /* Exercise the existing end path after consuming only half the pages. */
    struct uml_fault_pool pool = {0};
    assert(returned <= 32);
    memcpy(pool.local_pages, dest, returned * sizeof(*dest));
    pool.pages = pool.local_pages; pool.count = returned; pool.used = returned / 2;
    current->thread.fault_pool = &pool;
    uml_fault_pool_end(&pool);
    assert(!current->thread.fault_pool && !pool.pages && frees == returned - returned / 2);
    for (unsigned i = 0; i < returned; i++) {
        assert(dest[i]->refs == (i < returned / 2));
        assert(dest[i]->freed == (i >= returned / 2));
    }
    uml_fault_pool_end(&pool);
    assert(frees == returned - returned / 2);
}
int main(void) {
    for (unsigned count = 0; count <= 32; count++) {
        reset(count);
        unsigned n = uml_fault_pool_allocate(supplied_gfp, count, dest, 32);
        assert(n == count && high_pages + (count & 1) == count);
        assert(bulk_calls == (count & 1) && splits == calls);
        verify_and_release(n);
    }
    reset(30);
    supplied_gfp |= __GFP_RECLAIM | __GFP_COMP | __GFP_HIGH | __GFP_MEMALLOC;
    assert(uml_fault_pool_allocate(supplied_gfp, 30, dest, 32) == 30);
    assert(calls == 4 && orders[0] == 4 && orders[1] == 3 && orders[2] == 2 && orders[3] == 1);
    assert(!bulk_calls); verify_and_release(30);
    for (unsigned failure = 1; failure <= 4; failure++) {
        for (unsigned limit = 0; limit <= 4; limit++) {
            reset(30); fail_at = failure; bulk_limit = limit;
            unsigned n = uml_fault_pool_allocate(supplied_gfp, 30, dest, 32);
            assert(calls == failure && splits + 1 == calls && bulk_calls == 1);
            unsigned expected_high = failure == 1 ? 0 : failure == 2 ? 16 : failure == 3 ? 24 : 28;
            unsigned tail = 30 - expected_high;
            assert(high_pages == expected_high && n == high_pages + (limit < tail ? limit : tail));
            verify_and_release(n);
        }
    }
    for (unsigned excluded = 0; excluded < 3; excluded++) {
        reset(30);
        if (excluded == 0) uml_fault_contiguous = 0;
        if (excluded == 1) numa = 1;
        unsigned n = uml_fault_pool_allocate(supplied_gfp, 30, dest, excluded == 2 ? 64 : 32);
        assert(n == 30 && !calls && !splits && bulk_calls == 1);
        verify_and_release(n);
    }
    reset(33); bulk_limit = 7;
    unsigned n = uml_fault_pool_allocate(supplied_gfp, 33, dest, 64);
    assert(n == 7 && !calls && !splits && bulk_calls == 1);
    verify_and_release(n);
}
'''
            c_file = root / 'contiguous.c'
            c_file.write_text(program)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(c_file), '-o', str(root / 'contiguous')], check=True)
            subprocess.run([str(root / 'contiguous')], check=True, timeout=10)
