"""Host ownership must stay with a PTE slot, never its copied value."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class FaultHostmapTests(unittest.TestCase):
    def test_identity_and_protection_queue(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-hostmap.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helpers = []
        for start in ('static inline unsigned long uml_host_pte_value', 'int uml_host_protect('):
            pos = added.rindex(start)
            helpers.append(added[pos:added.index('\n}', pos) + 2])
        source = '''
#include <assert.h>
#include <stddef.h>
#include <string.h>
#define PAGE_MASK (~4095UL)
#define _PAGE_PRESENT 1
#define _PAGE_NEEDSYNC 2
#define _PAGE_HOST 4
#define _PAGE_PROTNONE 16
#define RW 32
#define STUB_SYSCALL_MPROTECT 6
struct stub_syscall { struct { unsigned long addr, length; int prot; } mem; int syscall; };
struct mm_id { struct stub_syscall queue[8]; unsigned count; };
static struct stub_syscall *syscall_stub_get_previous(struct mm_id *id, int op, unsigned long addr) {
    if (!id->count) return NULL;
    struct stub_syscall *sc = &id->queue[id->count - 1];
    return sc->syscall == op && sc->mem.addr + sc->mem.length == addr ? sc : NULL;
}
static struct stub_syscall *syscall_stub_alloc(struct mm_id *id) {
    assert(id->count < 8); return &id->queue[id->count++];
}
''' + '\n'.join(helpers) + '''
int main(void) {
    unsigned long mapped = 4096 | _PAGE_PRESENT | _PAGE_HOST | RW;
    unsigned long copied = uml_host_pte_value(0, mapped, 1);
    assert(!(copied & _PAGE_HOST) && (copied & _PAGE_NEEDSYNC));
    assert(uml_host_pte_value(mapped, mapped, 1) == mapped);
    unsigned long protected = uml_host_pte_value(mapped, mapped & ~RW, 1);
    assert((protected & (_PAGE_HOST | _PAGE_NEEDSYNC)) == (_PAGE_HOST | _PAGE_NEEDSYNC));
    assert(!(protected & RW));
    assert(uml_host_pte_value(protected, protected, 1) & _PAGE_NEEDSYNC);
    for (unsigned long pfn = 0; pfn < 8; pfn++) {
        unsigned long value = pfn * 4096 | _PAGE_PRESENT | _PAGE_HOST;
        unsigned long result = uml_host_pte_value(mapped, value, 1);
        assert(!!(result & _PAGE_HOST) == (pfn == 1));
        assert((result & PAGE_MASK) == pfn * 4096);
        assert(result & _PAGE_NEEDSYNC);
    }
    assert(!(uml_host_pte_value(mapped, 0, 1) & _PAGE_HOST));
    assert(!(uml_host_pte_value(mapped, 0x820, 1) & _PAGE_HOST));
    assert(uml_host_pte_value(mapped, mapped, 0) == (mapped | _PAGE_NEEDSYNC));
    unsigned long none = 4096 | _PAGE_PROTNONE;
    assert(uml_host_pte_value(mapped, none, 1) & _PAGE_HOST);
    struct mm_id id = {0};
    assert(!uml_host_protect(&id, 4096, 4096, 1));
    assert(!uml_host_protect(&id, 8192, 4096, 1));
    assert(id.count == 1 && id.queue[0].mem.length == 8192);
    assert(!uml_host_protect(&id, 16384, 4096, 1));
    assert(id.count == 2); /* Never span a hole. */
    assert(!uml_host_protect(&id, 20480, 4096, 3));
    assert(id.count == 3); /* Never merge different permissions. */
    assert(id.queue[2].mem.prot == 3);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'hostmap.c').write_text(source)
            subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'hostmap.c'), '-o', str(root / 'hostmap')], check=True)
            subprocess.run([str(root / 'hostmap')], check=True, timeout=10)
