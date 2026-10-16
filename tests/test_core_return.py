"""Runner-entry waits must not bypass return-to-user notifications."""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class CoreReturnTests(unittest.TestCase):
    def test_wait_and_allocation_recheck_work_without_holding_lease(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        name = 'arch/um/kernel/skas/process.c'
        def section(path):
            text = (config.REPO / path).read_text()
            return text.split('+++ b/' + name + '\n')[1].split('--- a/')[0]
        base = section('kernel/patches/0003-um-shared-vm-runners.patch')
        body = ''.join(line[1:] for line in base.splitlines(True) if line.startswith((' ', '+')))
        patch = section('kernel/candidates/core-syscall-patch.patch')
        for hunk in re.split(r'^@@[^\n]*\n', patch, flags=re.M)[1:]:
            old = ''.join(line[1:] for line in hunk.splitlines(True) if line.startswith((' ', '-')))
            new = ''.join(line[1:] for line in hunk.splitlines(True) if line.startswith((' ', '+')))
            if old in body:
                body = body.replace(old, new, 1)
        body = 'struct mm_id *current_mm_enter(void)' + body.split('struct mm_id *current_mm_enter(void)', 1)[1]
        body = body.split('\nvoid alarm_current_mm', 1)[0]
        self.assertIn('read_thread_flags()', body)
        self.assertIn('uml_patch_enter(mm)', body)
        source = r'''
#include <assert.h>
#include <stddef.h>
#include <stdlib.h>
#define GFP_KERNEL 0
#define __GFP_ZERO 0
#define STUB_THREAD_PAGES 4
#define STUB_THREAD_DATA(cpu) (0x1000UL * ((cpu) + 1))
#define _TIF_WORK_MASK 7
#define unlikely(x) (x)
#define guard(x) fake_guard
#define ilog2(x) 2
struct mm_id { unsigned long stack; };
struct mm_struct { struct { struct mm_id id, cpu_ids[2]; int sync_tlb_lock; } context; };
static struct mm_struct memory;
static struct { struct mm_struct *mm; } task = {&memory}, *current = &task;
static int parallel_userspace = 1, cpu, pinned, lease, flags, injections, resumes, mode, freed;
static void enter_turnstile(struct mm_id *id) { assert(id == &memory.context.id); }
static void exit_turnstile(struct mm_id *id) { assert(id == &memory.context.id); }
static void preempt_disable(void) { assert(!pinned); pinned = 1; }
static void preempt_enable(void) { assert(pinned); pinned = 0; }
static int raw_smp_processor_id(void) { return cpu; }
static struct mm_id *current_mm_id(void) { return &memory.context.cpu_ids[cpu]; }
static void uml_patch_enter(struct mm_struct *mm) {
    assert(mm == &memory && !lease && !pinned); lease = 1;
    if (mode == 1 && !injections++) { cpu = 1; flags = 3; }
}
static void uml_patch_exit(struct mm_struct *mm) { assert(mm == &memory && lease); lease = 0; }
static unsigned long __get_free_pages(int ignored, int order) {
    (void)ignored; assert(order == 2 && !pinned && lease && !injections++);
    cpu = 1; flags = 5; return 0x8000;
}
static void free_pages(unsigned long stack, int order) { assert(stack == 0x8000 && order == 2); freed++; }
static void fake_guard(int *lock) { (void)lock; assert(pinned); }
static int start_userspace_cpu(struct mm_id *broker, struct mm_id *id, unsigned long addr) {
    assert(broker == &memory.context.id && id == current_mm_id() && addr == STUB_THREAD_DATA(cpu)); return 0;
}
static void panic(const char *message, ...) { (void)message; abort(); }
static int read_thread_flags(void) { assert(pinned && lease); return flags; }
static void interrupt_end(void) { assert(!pinned && !lease && flags); flags = 0; resumes++; }
void current_mm_exit(struct mm_id *id);
''' + body + r'''
int main(void) {
    for (mode = 0; mode < 3; mode++) {
        cpu = pinned = lease = flags = injections = resumes = freed = 0;
        memory.context.cpu_ids[0].stack = mode == 2 ? 0 : 0x1000;
        memory.context.cpu_ids[1].stack = 0x2000;
        struct mm_id *id = current_mm_enter();
        assert(pinned && lease && !flags && id == current_mm_id());
        assert(cpu == !!mode && resumes == !!mode && freed == (mode == 2));
        current_mm_exit(id); assert(!pinned && !lease);
    }
    parallel_userspace = 0;
    struct mm_id *id = current_mm_enter();
    assert(id == &memory.context.id && !pinned && !lease); current_mm_exit(id);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)
