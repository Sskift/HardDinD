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
    def test_runner_wait_retries_interrupts_and_checks_liveness(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/patches/0003-um-shared-vm-runners.patch').read_text()
        part = patch.split('+++ b/arch/um/os-Linux/skas/process.c\n')[1].split('--- a/')[0]
        code = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        body = code.split('static int stub_exited(', 1)[1].split('\n}', 1)[0]
        patch = (config.REPO / 'kernel/candidates/core-syscall-patch.patch').read_text()
        part = patch.split('+++ b/arch/um/os-Linux/process.c\n')[1].split('--- a/')[0]
        code = '\n'.join(line[1:] for line in part.splitlines() if line.startswith(('+', ' ')))
        reap = code.split('pid_t os_reap_child(', 1)[1].split('\n}', 1)[0]
        source = r'''
#include <assert.h>
#include <errno.h>
#include <signal.h>
#include <sys/wait.h>
#ifndef __WALL
#define __WALL 0x40000000
#endif
#define CATCH_EINTR(x) do { x; } while (ret < 0 && errno == EINTR)
static int interrupts, probe_interrupts, wait_error, probe_error, exited, waits, probes;
static int test_waitid(int kind, int pid, siginfo_t *state, int flags) {
    assert(kind == P_PID && pid == 42 && flags == (WEXITED | WNOHANG | WNOWAIT | __WALL));
    waits++;
    if (interrupts) { interrupts--; errno = EINTR; return -1; }
    if (wait_error) { errno = wait_error; return -1; }
    state->si_pid = exited ? pid : 0;
    return 0;
}
static int test_kill(int pid, int sig) {
    assert(pid == 42 && !sig);
    probes++;
    if (probe_interrupts) { probe_interrupts--; errno = EINTR; return -1; }
    if (probe_error) { errno = probe_error; return -1; }
    return 0;
}
#define waitid test_waitid
#define kill test_kill
static int stub_exited(''' + body + r'''
}
static int test_waitpid(int pid, int *status, int flags) {
    assert(pid == -1 && flags == WNOHANG);
    waits++;
    if (interrupts) { interrupts--; errno = EINTR; return -1; }
    *status = 7 << 8;
    return 42;
}
#define waitpid test_waitpid
pid_t os_reap_child(''' + reap + r'''
}
int main(void) {
    const struct { int wait_error, probe_error, exited, dead; } cases[] = {
        {0, 0, 0, 0},                 /* live waitable child */
        {0, 0, 1, 1},                 /* retained exit status */
        {ECHILD, 0, 0, 0},            /* live non-waitable runner */
        {ECHILD, ESRCH, 0, 1},        /* reaped runner */
        {ECHILD, EPERM, 0, 0},        /* existence probe denied */
        {ECHILD, EINVAL, 0, 0},       /* unexpected probe failure */
        {EINVAL, ESRCH, 0, 0},        /* wait failed without exit evidence */
    };
    for (unsigned int i = 0; i < sizeof(cases) / sizeof(*cases); i++) {
        siginfo_t state = {0};
        interrupts = probe_interrupts = 3;
        wait_error = cases[i].wait_error;
        probe_error = cases[i].probe_error;
        exited = cases[i].exited;
        waits = probes = 0;
        assert(stub_exited(42, &state) == cases[i].dead);
        assert(waits == 4 && probes == (wait_error == ECHILD ? 4 : 0));
    }
    for (int pid = -1; pid <= 0; pid++) {
        siginfo_t state = {0};
        waits = probes = 0;
        assert(stub_exited(pid, &state));
        assert(waits == 0 && probes == 0);
    }
    int status = 0;
    waits = 0; interrupts = 3;
    assert(os_reap_child(&status) == 42 && waits == 4);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 7);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)

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
#define UML_PATCH_BASE 0x06060000
#define UML_PATCH_END 0x06070000
#define UML_PATCH_SLOT 256
#define UM_SECCOMP_ARCH_NATIVE 0xc000003eU
enum { BPF_LD=0, BPF_W=0, BPF_ABS=0x20, BPF_ALU=4, BPF_AND=0x50, BPF_JMP=5, BPF_JEQ=0x10, BPF_JGE=0x30, BPF_K=0, BPF_RET=6,
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
        } else if (f.op == (BPF_ALU | BPF_AND)) {
            a &= f.k;
        } else {
            assert(f.op == (BPF_JMP | BPF_JEQ) || f.op == (BPF_JMP | BPF_JGE));
            i += (f.op == (BPF_JMP | BPF_JEQ) ? a == f.k : a >= f.k) ? f.yes : f.no;
        }
    }
    assert(0); return 0;
}
int main(void) {
    const uint64_t ips[] = {UML_PATCH_BASE - 1, UML_PATCH_BASE, UML_PATCH_END - 1,
                            UML_PATCH_END, 0x100000000ULL + UML_PATCH_BASE,
                            UML_PATCH_BASE + 143, UML_PATCH_BASE + 192, UML_PATCH_BASE + 234,
                            UML_PATCH_END - UML_PATCH_SLOT + 143, UML_PATCH_END - UML_PATCH_SLOT + 192};
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
                int allowed = enabled && nr == __NR_gettid && ips[i] >= UML_PATCH_BASE && ips[i] < UML_PATCH_END &&
                    (ips[i] % UML_PATCH_SLOT == 143 || ips[i] % UML_PATCH_SLOT == 192);
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
        source = '#include <assert.h>\n#include <string.h>\n#include "core-patch.h"\n' + r"""
int main(void) {
    unsigned char a[] = {0xb8,39,0,0,0,0x0f,0x05};
    unsigned char b[] = {0x48,0xc7,0xc0,39,0,0,0,0x0f,0x05};
    assert(uml_patch_pattern(a, 7, 39));
    assert(uml_patch_pattern(b, 9, 39));
    assert(!uml_patch_pattern(a, 7, 40));
    assert(!uml_patch_pattern(b, 9, 40));
    assert(!uml_patch_pattern(0, 0, 39));
    assert(!uml_patch_pattern(a, 8, 39));
    unsigned char go[] = {0x49,0x89,0xf2,0x48,0x89,0xfa,0x48,0x89,0xce,0x48,0x89,0xdf,0x0f,0x05};
    unsigned char generic[] = {0x4c,0x8b,0x4c,0x24,8,0x0f,0x05};
    unsigned char abi0[] = {0x48,0x8b,0x44,0x24,8,0x0f,0x05};
    assert(uml_patch_dynamic(go, sizeof(go)));
    assert(uml_patch_dynamic(generic, sizeof(generic)));
    assert(uml_patch_dynamic(abi0, sizeof(abi0)));
    for (unsigned int i = 0; i < sizeof(go); i++) {
        go[i] ^= 1; assert(!uml_patch_dynamic(go, sizeof(go))); go[i] ^= 1;
    }
    for (unsigned long slot = UML_PATCH_BASE + UML_PATCH_SLOT; slot < UML_PATCH_END; slot += UML_PATCH_SLOT) {
        unsigned char jump[14];
        uml_patch_jump(jump, sizeof(jump), slot);
        assert(jump[3] == 6 && jump[5] == 6 && jump[6] == 6 && jump[7] == 6 && jump[9] == 6 && jump[12] == 6);
    }
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
            (path / 'core-patch.h').write_text(header)
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
        dynamic = kernel.split('    if (dynamic) {\n        unsigned int at', 1)[1].split('\n    }', 1)[0]
        source = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <signal.h>
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
static unsigned long expected_slow;
static unsigned int fast_count, slow_count;
static void slow_return(int sig, siginfo_t *info, void *context) {
    (void)info;
    mcontext_t *mc = &((ucontext_t *)context)->uc_mcontext;
    assert(sig == SIGILL && (unsigned long)mc->gregs[REG_RIP] == expected_slow);
    assert(mc->gregs[REG_RAX] == getpid());
    mc->gregs[REG_RAX] = 123;
    mc->gregs[REG_RIP] = expected_rip;
    mc->gregs[REG_RCX] = expected_rip;
    slow_count++;
}
void *stub_fast_handoff(void *d) {
    mcontext_t *mc = d + UML_FAST_MC;
    assert(mc->gregs[REG_RAX] == 39);
    assert(mc->gregs[REG_RDI] == 11 && mc->gregs[REG_RSI] == 22);
    assert(mc->gregs[REG_RDX] == 33 && mc->gregs[REG_R10] == 44);
    assert(mc->gregs[REG_R8] == 55 && mc->gregs[REG_R9] == 66);
    assert((unsigned long)mc->gregs[REG_RIP] == expected_rip);
    assert(mc->gregs[REG_RCX] == mc->gregs[REG_RIP]);
    assert(mc->gregs[REG_R11] == mc->gregs[REG_EFL]);
    assert((mc->gregs[REG_EFL] & 0x401) == 0x401);
    fast_count++;
    mc->gregs[REG_RAX] = 123;
    __asm__ volatile("pxor %%xmm15,%%xmm15" ::: "xmm15");
    return d;
}
''' + assembly + r'''
__asm__(".text\n.global leaf_start, leaf_site, leaf_after, leaf_end\n"
        "leaf_start: mov %rcx,%r10; movabs $0x123456789abcdef0,%rax\n"
        ".set offset,8\n.rept 16\nmov %rax,-offset(%rsp)\n.set offset,offset+8\n.endr\n"
        "stc; std; mov $39,%eax\nleaf_site: mov $39,%eax; syscall\n"
        "leaf_after: cld; jnc 1f; movabs $0x123456789abcdef0,%r11\n"
        ".set offset,8\n.rept 16\ncmp %r11,-offset(%rsp); jne 1f\n.set offset,offset+8\n.endr\n"
        "ret\n1: mov $-1,%rax; ret\nleaf_end:\n");
int main(void) {
    unsigned char *d = mmap(0, 16384, 3, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char *tids = mmap(0, 4198400, 3, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char *table = mmap((void *)UML_PATCH_BASE, 65536, 7,
                               MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    unsigned char *leaf = mmap(0, 4096, 3, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(d != MAP_FAILED && tids != MAP_FAILED && table == (void *)UML_PATCH_BASE && leaf != MAP_FAILED);
    assert((uintptr_t)d % 64 == 0 && leaf_end - leaf_start < 4096);
    struct sigaction sa = {.sa_sigaction=slow_return, .sa_flags=SA_SIGINFO};
    assert(!sigemptyset(&sa.sa_mask) && !sigaction(SIGILL, &sa, NULL));
    expected_slow = (uintptr_t)table + UML_PATCH_SLOT + 234;
    long tid = syscall(SYS_gettid); assert(tid > 0 && tid < 4194304); tids[tid] = 1;
    memcpy(leaf, leaf_start, leaf_end - leaf_start);
    expected_rip = (unsigned long)leaf + (leaf_after - leaf_start);
    unsigned char code[UML_PATCH_SLOT] = {''' + emitter + r'''};
    uint64_t value = (uintptr_t)table + UML_PATCH_SLOT + 136; memcpy(code + 6, &value, 8);
    uint32_t nr = 39; memcpy(code + 40, &nr, 4);
    memcpy(code + 48, &expected_rip, 8);
    value = (uintptr_t)tids; memcpy(code + 64, &value, 8);
    value = (uintptr_t)d - 16384; memcpy(code + 72, &value, 8);
    value = (uintptr_t)stub_fast_entry; memcpy(code + 80, &value, 8);
    unsigned char jump[7]; uml_patch_jump(jump, 7, (uintptr_t)table + UML_PATCH_SLOT);
    memcpy(leaf + (leaf_site - leaf_start), jump, sizeof(jump));
    assert(!mprotect(leaf, 4096, PROT_READ | PROT_EXEC));
    unsigned long pattern[2] = {0x1122334455667788UL, 0x8877665544332211UL}, result[2];
    for (int mode = 0; mode < 2; mode++) {
      if (mode) {
        typedef int32_t s32;
        #define STUB_TID_TABLE ((uintptr_t)tids)
        #define STUB_TID_SLOTS 4194304
        unsigned int len = 7;
        unsigned char old[14] = {[7]=0x4c,0x8b,0x4c,0x24,8,0x0f,0x05};
        unsigned int at''' + dynamic + r'''
        code[44] = 1;
      }
      memcpy(table + UML_PATCH_SLOT, code, sizeof(code));
      for (int phase = 0; phase < 2; phase++) {
      if (phase && !__builtin_cpu_supports("avx")) continue;
      *(uint64_t *)(d + 56) = phase ? 7 : 0;
      for (int i = 0; i < 1024; i++) {
        __asm__ volatile("movdqu %0,%%xmm15" :: "m"(pattern) : "xmm15");
        long value = ((long (*)(long,long,long,long,long,long,long))leaf)(11,22,33,44,55,66,66);
        __asm__ volatile("movdqu %%xmm15,%0" : "=m"(result));
        assert(value == 123 && !memcmp(result, pattern, sizeof(pattern)));
      }
      if (mode) {
        unsigned int saved_count = fast_count;
        *(uint64_t *)(tids + STUB_TID_SLOTS) = 1;
        long value = ((long (*)(long,long,long,long,long,long,long))leaf)(11,22,33,44,55,66,66);
        assert(value == 123 && fast_count == saved_count);
        assert(*(uint64_t *)(tids + STUB_TID_SLOTS) == 1);
        *(uint64_t *)(tids + STUB_TID_SLOTS) = 0;
      }
    }
    }
    assert(fast_count >= 2048 && slow_count >= 1);
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
    def test_native_dynamic_register_wrappers(self):
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            binary = pathlib.Path(directory) / 'core_dynamic'
            subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                            str(config.REPO / 'src/core_dynamic.c'), '-o', str(binary)], check=True)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            row = json.loads(run.stdout)
            self.assertEqual(row['schema'], 'hd.core-dynamic/1')
            self.assertEqual(row['calls'], 98304)
            self.assertEqual(row['prefix_faults'], 2)
            self.assertGreater(row['signals'], 0)
            for key in ('restart', 'arguments', 'fork', 'exec', 'verified'):
                self.assertIs(row[key], True)
            self.assertIs(row['enabled'], False)

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
