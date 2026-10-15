"""Exercise the complete rewriting checker on a native Linux process."""
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class CorePatchTests(unittest.TestCase):
    def test_canonical_instruction_decoder(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-syscall-patch.patch').read_text()
        part = patch.split('+++ b/arch/um/include/shared/core-patch.h\n')[1].split('--- a/')[0]
        header = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        source = '#include <assert.h>\n#include <string.h>\n' + header + r"""
int main(void) {
    unsigned char a[] = {0xb8,39,0,0,0,0x0f,0x05};
    unsigned char b[] = {0x48,0xc7,0xc0,39,0,0,0,0x0f,0x05};
    assert(uml_patch_pattern(a, 7, 39));
    assert(uml_patch_pattern(b, 9, 39));
    assert(!uml_patch_pattern(a, 7, 40));
    assert(!uml_patch_pattern(b, 9, 40));
    assert(!uml_patch_pattern(0, 0, 39));
    assert(!uml_patch_pattern(a, 8, 39));
    for (int i = 0; i < 7; i++) {
        a[i] ^= 1;
        assert(!uml_patch_pattern(a, 7, 39));
        a[i] ^= 1;
    }
    for (int i = 0; i < 9; i++) {
        b[i] ^= 1;
        assert(!uml_patch_pattern(b, 9, 39));
        b[i] ^= 1;
    }
}
"""
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64',
                         'Linux x86-64 trampoline execution')
    def test_actual_trampoline_preserves_arguments_fp_and_red_zone(self):
        patch = (config.REPO / 'kernel/candidates/core-syscall-patch.patch').read_text()
        def added(path):
            part = patch.split('+++ b/' + path + '\n')[1].split('--- a/')[0]
            return '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        header = added('arch/um/include/shared/core-patch.h')
        stub = added('arch/um/kernel/skas/stub.c')
        assembly = stub[stub.rindex('asm(\n'):].split(');', 1)[0] + ');\n'
        kernel = added('arch/um/kernel/core-patch.c')
        emitter = kernel.split('unsigned char code[UML_PATCH_SLOT] = {', 1)[1].split('};', 1)[0]
        source = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>
''' + header + r'''
extern void stub_fast_entry(void);
extern unsigned char leaf_start[], leaf_site[], leaf_after[], leaf_end[];
static unsigned long expected_rip;
void *stub_fast_handoff(void *d) {
    mcontext_t *mc = d + UML_FAST_MC;
    assert(mc->gregs[REG_RAX] == 39);
    assert(mc->gregs[REG_RDI] == 11 && mc->gregs[REG_RSI] == 22);
    assert(mc->gregs[REG_RDX] == 33 && mc->gregs[REG_R10] == 44);
    assert(mc->gregs[REG_R8] == 55 && mc->gregs[REG_R9] == 66);
    assert((unsigned long)mc->gregs[REG_RIP] == expected_rip);
    assert(mc->gregs[REG_RCX] == mc->gregs[REG_RIP]);
    assert(mc->gregs[REG_R11] == mc->gregs[REG_EFL]);
    mc->gregs[REG_RAX] = 123;
    __asm__ volatile("pxor %%xmm15,%%xmm15" ::: "xmm15");
    return d;
}
''' + assembly + r'''
__asm__(".text\n.global leaf_start, leaf_site, leaf_after, leaf_end\n"
        "leaf_start: mov %rcx,%r10; movabs $0x123456789abcdef0,%rax\n"
        ".set offset,8\n.rept 16\nmov %rax,-offset(%rsp)\n.set offset,offset+8\n.endr\n"
        "leaf_site: mov $39,%eax; syscall\n"
        "leaf_after: movabs $0x123456789abcdef0,%r11\n"
        ".set offset,8\n.rept 16\ncmp %r11,-offset(%rsp); jne 1f\n.set offset,offset+8\n.endr\n"
        "ret\n1: mov $-1,%rax; ret\nleaf_end:\n");
int main(void) {
    unsigned char *d = mmap(0, 16384, 3, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char *tids = mmap(0, 4194304, 3, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char *table = mmap((void *)UML_PATCH_BASE, 65536, 7,
                               MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    unsigned char *leaf = mmap(0, 4096, 3, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(d != MAP_FAILED && tids != MAP_FAILED && table == (void *)UML_PATCH_BASE && leaf != MAP_FAILED);
    assert((uintptr_t)d % 64 == 0 && leaf_end - leaf_start < 4096);
    long tid = syscall(SYS_gettid); assert(tid > 0 && tid < 4194304); tids[tid] = 1;
    memcpy(leaf, leaf_start, leaf_end - leaf_start);
    expected_rip = (unsigned long)leaf + (leaf_after - leaf_start);
    unsigned char code[UML_PATCH_SLOT] = {''' + emitter + r'''};
    uint64_t value = (uintptr_t)table + 128 + 8; memcpy(code, &value, 8);
    uint32_t nr = 39; memcpy(code + 40, &nr, 4);
    memcpy(code + 48, &expected_rip, 8);
    value = (uintptr_t)tids; memcpy(code + 64, &value, 8);
    value = (uintptr_t)d - 16384; memcpy(code + 72, &value, 8);
    value = (uintptr_t)stub_fast_entry; memcpy(code + 80, &value, 8);
    memcpy(table + 128, code, sizeof(code));
    unsigned char jump[] = {0xff,0x24,0x25,0x80,0x00,0x06,0x00};
    memcpy(leaf + (leaf_site - leaf_start), jump, sizeof(jump));
    assert(!mprotect(leaf, 4096, PROT_READ | PROT_EXEC));
    unsigned long pattern[2] = {0x1122334455667788UL, 0x8877665544332211UL}, result[2];
    for (int phase = 0; phase < 2; phase++) {
      if (phase && !__builtin_cpu_supports("avx")) continue;
      *(uint64_t *)(d + 56) = phase ? 7 : 0;
      for (int i = 0; i < 1024; i++) {
        __asm__ volatile("movdqu %0,%%xmm15" :: "m"(pattern) : "xmm15");
        long value = ((long (*)(long,long,long,long,long,long))leaf)(11,22,33,44,55,66);
        __asm__ volatile("movdqu %%xmm15,%0" : "=m"(result));
        assert(value == 123 && !memcmp(result, pattern, sizeof(pattern)));
    }
    }
    puts("trampoline-ok");
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', path / 'test.c',
                            '-o', path / 'test'], check=True)
            result = subprocess.run([path / 'test'], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, 'trampoline-ok\n')

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64',
                         'Linux x86-64 ptrace and instruction encoding')
    def test_native_private_text_signals_fork_exec_and_replacement(self):
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            binary = pathlib.Path(directory) / 'core_patch'
            subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                            str(config.REPO / 'src/core_patch.c'), '-o', str(binary)], check=True)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            row = json.loads(run.stdout)
            self.assertEqual(row['schema'], 'hd.core-patch/1')
            self.assertEqual(row['sites'], 32)
            self.assertEqual(row['calls'], 131072)
            self.assertGreater(row['signals'], 0)
            for key in ('fork', 'exec_clean', 'file_clean', 'ptrace', 'restart', 'long_form', 'ambiguous_fallback', 'mremap', 'discard_remap_write', 'verified'):
                self.assertIs(row[key], True)
            self.assertIs(row['enabled'], False)
