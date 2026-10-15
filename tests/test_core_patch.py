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
    def test_runner_gettid_does_not_bypass_allowlist(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-syscall-patch.patch').read_text()
        part = patch.split('+++ b/arch/um/kernel/skas/stub_exe.c\n')[1].split('--- a/')[0]
        code = '\n'.join(line[1:] for line in part.splitlines() if line.startswith(('+', ' ')))
        prefix = code.split('struct sock_filter filter[] = {')[1].split('#endif')[0]
        prefix = prefix.replace('#ifdef __x86_64__', '')
        fragment = code.split('/* Each match is followed by its action: no fragile jump counts. */')[1]
        fragment = fragment.split('BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_recvmsg')[0]
        source = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define UML_PATCH_BASE 0x60000
#define UML_PATCH_END 0x70000
#define UM_SECCOMP_ARCH_NATIVE 0xc000003eU
enum { BPF_LD=0, BPF_W=0, BPF_ABS=0x20, BPF_JMP=5, BPF_JEQ=0x10, BPF_JGE=0x30, BPF_K=0, BPF_RET=6,
       __NR_getpid=39, __NR_gettid=186, __NR_futex=202,
       SECCOMP_RET_ALLOW=10, SECCOMP_RET_TRAP=11, SECCOMP_RET_KILL_PROCESS=12 };
struct instruction { unsigned int op, k, yes, no; };
struct seccomp_data { int nr; unsigned int arch; uint64_t instruction_pointer; };
#define BPF_JUMP(op,k,y,n) {op,k,y,n}
#define BPF_STMT(op,k) {op,k,0,0}
static unsigned int evaluate(unsigned int nr, int enabled, uint64_t ip, unsigned int arch, int trampoline) {
    struct { int syscall_patch; } init_data = {enabled};
    struct instruction stub_filter[] = {
''' + fragment + r'''
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS)
    };
    struct instruction entry_filter[] = {
''' + prefix + r'''
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP)
    };
    struct instruction *filter = trampoline ? entry_filter : stub_filter;
    unsigned int count = (trampoline ? sizeof(entry_filter) : sizeof(stub_filter)) / sizeof(*filter);
    struct seccomp_data input = {(int)nr, arch, ip};
    unsigned int a = nr;
    for (unsigned int i = 0; i < count; i++) {
        struct instruction f = filter[i];
        if (f.op == BPF_RET) return f.k;
        if (f.op == (BPF_LD | BPF_W | BPF_ABS)) {
            assert(f.k <= sizeof(input) - sizeof(a));
            memcpy(&a, (char *)&input + f.k, sizeof(a));
        } else {
            assert(f.op == (BPF_JMP | BPF_JEQ) || f.op == (BPF_JMP | BPF_JGE));
            i += (f.op == (BPF_JMP | BPF_JEQ) ? a == f.k : a >= f.k) ? f.yes : f.no;
        }
    }
    assert(0); return 0;
}
int main(void) {
    const uint64_t ips[] = {UML_PATCH_BASE - 1, UML_PATCH_BASE, UML_PATCH_END - 1,
                            UML_PATCH_END, 0x100000000ULL + UML_PATCH_BASE};
    for (int enabled = 0; enabled < 2; enabled++) {
        assert(evaluate(__NR_getpid, enabled, 0, 0, 0) == SECCOMP_RET_TRAP);
        assert(evaluate(__NR_gettid, enabled, 0, 0, 0) == (enabled ? SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS));
        assert(evaluate(__NR_futex, enabled, 0, 0, 0) == SECCOMP_RET_ALLOW);
        assert(evaluate(~0U, enabled, 0, 0, 0) == SECCOMP_RET_KILL_PROCESS);
        for (unsigned int nr = 0; nr < 512; nr++)
            if (nr != __NR_getpid && nr != __NR_gettid && nr != __NR_futex)
                assert(evaluate(nr, enabled, 0, 0, 0) == SECCOMP_RET_KILL_PROCESS);
        for (unsigned int i = 0; i < sizeof(ips)/sizeof(*ips); i++)
            for (unsigned int nr = 0; nr < 512; nr++) {
                int allowed = enabled && nr == __NR_gettid && ips[i] >= UML_PATCH_BASE && ips[i] < UML_PATCH_END;
                assert(evaluate(nr, enabled, ips[i], UM_SECCOMP_ARCH_NATIVE, 1) ==
                       (allowed ? SECCOMP_RET_ALLOW : SECCOMP_RET_TRAP));
                assert(evaluate(nr, enabled, ips[i], 0, 1) == SECCOMP_RET_TRAP);
            }
        assert(evaluate(~0U, enabled, UML_PATCH_BASE, UM_SECCOMP_ARCH_NATIVE, 1) == SECCOMP_RET_TRAP);
    }
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)

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
            for key in ('fork', 'exec_clean', 'file_clean', 'ptrace', 'poketext', 'proc_mem', 'single_step', 'restart', 'long_form', 'ambiguous_fallback', 'mremap', 'discard_remap_write', 'verified'):
                self.assertIs(row[key], True)
            self.assertIs(row['enabled'], False)
