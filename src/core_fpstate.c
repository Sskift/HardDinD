/* FP preservation and deliberate signal/ptrace edits across syscall handoffs. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core FP failure line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define NT_X86_XSTATE 0x202
#define YMM15_HIGH (576 + 15 * 16)
struct vector { unsigned char bytes[32]; };
struct worker { unsigned long count; int cpu, avx, tag; };
static pthread_barrier_t barrier;
static volatile sig_atomic_t delivered;
static struct vector replacement;
static int patch_enabled;
static unsigned int hot;

static double now(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &ts));
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

struct lazy_counts { uint64_t enabled, borrowed, reused, materialized; };
static struct lazy_counts lazy_counts(void) {
    struct lazy_counts counts = {0};
    FILE *f = fopen("/proc/uml_fp_lazy", "r");
    if (!f) { CHECK(errno == ENOENT); return counts; }
    char key[64]; uint64_t value;
    while (fscanf(f, "%63s %" SCNu64, key, &value) == 2) {
        if (!strcmp(key, "enabled")) { CHECK(value <= 1); counts.enabled = value; }
        else if (!strcmp(key, "borrowed")) counts.borrowed = value;
        else if (!strcmp(key, "reused")) counts.reused = value;
        else if (!strcmp(key, "materialized")) counts.materialized = value;
        else CHECK(0);
    }
    CHECK(feof(f) && !ferror(f) && !fclose(f));
    return counts;
}

static uint64_t fast_calls(void) {
    FILE *f = fopen("/proc/uml_syscall_patch", "r");
    if (!f) { CHECK(errno == ENOENT); return 0; }
    char key[64]; uint64_t value, calls = 0;
    while (fscanf(f, "%63s %" SCNu64, key, &value) == 2) {
        if (!strcmp(key, "enabled")) { CHECK(value <= 1); patch_enabled = (int)value; }
        if (!strcmp(key, "fast_calls")) calls = value;
        if (!strcmp(key, "hot_threshold")) { CHECK(value <= 65535); hot = (unsigned int)value; }
    }
    CHECK(feof(f) && !ferror(f) && !fclose(f));
    return calls;
}

static struct vector pattern(int tag) {
    struct vector v;
    for (unsigned int i = 0; i < sizeof(v.bytes); i++) v.bytes[i] = (unsigned char)(tag + i * 17);
    return v;
}

#define CALL_LOOP \
    "1: .rept 14; nop; .endr; mov $39, %%eax; syscall; cmp %[pid], %%rax; je 2f; mov $1, %[bad]; 2:\n" \
    "dec %[n]; jz 3f; test $127, %[n]; jnz 1b; mov $24, %%eax; syscall; jmp 1b; 3:\n"
static __attribute__((target("avx"))) void
preserve(const struct vector *in, struct vector *out, unsigned long count, int avx) {
    unsigned long bad = 0, n = count;
    long pid = syscall(SYS_getpid);
    if (avx) {
        __asm__ volatile("vmovdqu %[in], %%ymm15\n" CALL_LOOP "vmovdqu %%ymm15, %[out]; vzeroupper"
                         : [n] "+&r"(n), [bad] "+&r"(bad), [out] "=m"(*out)
                         : [in] "m"(*in), [pid] "r"(pid)
                         : "rax", "rcx", "r11", "ymm15", "cc", "memory");
    } else {
        __asm__ volatile("movdqu %[in], %%xmm15\n" CALL_LOOP "movdqu %%xmm15, %[out]"
                         : [n] "+&r"(n), [bad] "+&r"(bad), [out] "=m"(*out)
                         : [in] "m"(*in), [pid] "r"(pid)
                         : "rax", "rcx", "r11", "xmm15", "cc", "memory");
    }
    CHECK(!bad && !memcmp(in, out, avx ? 32 : 16));
}
static void *worker(void *arg) {
    struct worker *w = arg;
    cpu_set_t cpus;
    CPU_ZERO(&cpus); CPU_SET(w->cpu, &cpus);
    CHECK(!pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus));
    struct vector in = pattern(w->tag), out = {{0}};
    int r = pthread_barrier_wait(&barrier);
    CHECK(!r || r == PTHREAD_BARRIER_SERIAL_THREAD);
    preserve(&in, &out, w->count, w->avx);
    return NULL;
}
static void threads(unsigned long count, int avx) {
    pthread_t ids[2]; struct worker w[2]; cpu_set_t allowed;
    CHECK(!sched_getaffinity(0, sizeof(allowed), &allowed));
    int cpu;
    for (cpu = 0; cpu < CPU_SETSIZE && !CPU_ISSET(cpu, &allowed); cpu++) {}
    CHECK(cpu < CPU_SETSIZE && !pthread_barrier_init(&barrier, NULL, 2));
    for (int i = 0; i < 2; i++) {
        w[i] = (struct worker){count, cpu, avx, 31 + 71 * i};
        CHECK(!pthread_create(&ids[i], NULL, worker, &w[i]));
    }
    for (int i = 0; i < 2; i++) CHECK(!pthread_join(ids[i], NULL));
    CHECK(!pthread_barrier_destroy(&barrier));
}
static __attribute__((target("avx"))) void
stop_with_vector(int sig, const struct vector *in, struct vector *out) {
    long pid = syscall(SYS_getpid), tid = syscall(SYS_gettid), result;
    __asm__ volatile("vmovdqu %[in], %%ymm15; .rept 14; nop; .endr; mov $234, %%eax; syscall; vmovdqu %%ymm15, %[out]; vzeroupper"
                     : "=a"(result), [out] "=m"(*out)
                     : "D"(pid), "S"(tid), "d"((long)sig), [in] "m"(*in)
                     : "rcx", "r11", "ymm15", "cc", "memory");
    CHECK(!result);
}
static void signal_edit(int sig, siginfo_t *info, void *context) {
    (void)info;
    unsigned char *fp = (void *)((ucontext_t *)context)->uc_mcontext.fpregs;
    uint32_t magic; uint64_t features;
    if (sig != SIGUSR1 || !fp) _exit(90);
    memcpy(&magic, fp + 464, sizeof(magic));
    if (magic != 0x46505853U) _exit(91);
    memcpy(&features, fp + 512, sizeof(features));
    if (!(features & 4)) _exit(92);
    memcpy(fp + YMM15_HIGH, replacement.bytes + 16, 16);
    delivered++;
}
static void signals(void) {
    struct vector in = pattern(19), out = {{0}}, expected = in;
    replacement = pattern(117);
    memcpy(expected.bytes + 16, replacement.bytes + 16, 16);
    struct sigaction sa = {.sa_sigaction = signal_edit, .sa_flags = SA_SIGINFO};
    sigemptyset(&sa.sa_mask);
    CHECK(!sigaction(SIGUSR1, &sa, NULL));
    for (int n = 1; n <= 2; n++) {
        stop_with_vector(SIGUSR1, &in, &out);
        CHECK(delivered == n && !memcmp(&out, &expected, sizeof(out)));
    }
}
static void traced(void) {
    struct vector in = pattern(43), expected = in;
    replacement = pattern(151);
    memcpy(expected.bytes + 16, replacement.bytes + 16, 16);
    struct vector *out = mmap(NULL, sizeof(*out), PROT_READ | PROT_WRITE,
                              MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(out != MAP_FAILED);
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        CHECK(!ptrace(PTRACE_TRACEME, 0, NULL, NULL));
        stop_with_vector(SIGSTOP, &in, out);
        _exit(0);
    }
    int status; CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFSTOPPED(status) && WSTOPSIG(status) == SIGSTOP);
    CHECK(!ptrace(PTRACE_SETOPTIONS, child, NULL, (void *)(uintptr_t)PTRACE_O_EXITKILL));
    unsigned char *fp = calloc(1, 65536); CHECK(fp != NULL);
    struct iovec io = {.iov_base = fp, .iov_len = 65536};
    CHECK(!ptrace(PTRACE_GETREGSET, child, (void *)(uintptr_t)NT_X86_XSTATE, &io));
    CHECK(io.iov_len >= YMM15_HIGH + 16 && io.iov_len <= 65536);
    uint64_t features; memcpy(&features, fp + 512, sizeof(features));
    CHECK(features & 4);
    memcpy(fp + YMM15_HIGH, replacement.bytes + 16, 16);
    CHECK(!ptrace(PTRACE_SETREGSET, child, (void *)(uintptr_t)NT_X86_XSTATE, &io));
    CHECK(!ptrace(PTRACE_CONT, child, NULL, NULL));
    CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
    CHECK(!memcmp(out, &expected, sizeof(*out)));
    free(fp); CHECK(!munmap(out, sizeof(*out)));
}
static void fatal_and_redirected(void) {
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        struct rlimit no_core = {0, 0};
        CHECK(!setrlimit(RLIMIT_CORE, &no_core));
        struct sock_filter code[] = {
            BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
            BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        };
        struct sock_fprog filter = {.len = sizeof(code) / sizeof(code[0]), .filter = code};
        CHECK(!prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0));
        CHECK(!prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter));
        syscall(SYS_getpid); _exit(99);
    }
    int status;
    CHECK(waitpid(child, &status, 0) == child && WIFSIGNALED(status) && WTERMSIG(status) == SIGSYS);
    child = fork(); CHECK(child >= 0);
    if (!child) {
        CHECK(!ptrace(PTRACE_TRACEME, 0, NULL, NULL));
        CHECK(!syscall(SYS_tgkill, getpid(), syscall(SYS_gettid), SIGSTOP));
        syscall(SYS_getpid); _exit(99);
    }
    CHECK(waitpid(child, &status, 0) == child && WIFSTOPPED(status) && WSTOPSIG(status) == SIGSTOP);
    CHECK(!ptrace(PTRACE_SETOPTIONS, child, NULL,
                 (void *)(uintptr_t)(PTRACE_O_EXITKILL | PTRACE_O_TRACESYSGOOD)));
    CHECK(!ptrace(PTRACE_SYSCALL, child, NULL, NULL));
    CHECK(waitpid(child, &status, 0) == child && WIFSTOPPED(status) && WSTOPSIG(status) == (SIGTRAP | 0x80));
    struct user_regs_struct regs;
    CHECK(!ptrace(PTRACE_GETREGS, child, NULL, &regs) && regs.orig_rax == SYS_getpid);
    regs.orig_rax = SYS_exit_group; regs.rdi = 37;
    CHECK(!ptrace(PTRACE_SETREGS, child, NULL, &regs));
    CHECK(!ptrace(PTRACE_CONT, child, NULL, NULL));
    CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 37);
}
int main(int argc, char **argv) {
    CHECK(argc == 2 || (argc == 3 && !strcmp(argv[2], "--timing")));
    char *end; errno = 0;
    unsigned long count = strtoul(argv[1], &end, 10);
    CHECK(!errno && *argv[1] && !*end && count >= 256 && count <= 1048576);
    if (!__builtin_cpu_supports("avx")) {
        fprintf(stderr, "AVX state unavailable; required checks not tested\n");
        return 77;
    }
    uint64_t before = fast_calls();
    if (patch_enabled && hot) {
        threads(hot + 1, 0); threads(hot + 1, 1);
        struct vector in = pattern(53), out;
        for (unsigned int i = 0; i < hot; i++) {
            stop_with_vector(0, &in, &out);
            CHECK(!memcmp(&in, &out, sizeof(in)));
        }
        before = fast_calls();
    }
    struct lazy_counts lazy_before = lazy_counts();
    double start = now();
    threads(count, 0);
    double sse_seconds = now() - start;
    start = now();
    threads(count, 1);
    double avx_seconds = now() - start;
    signals(); traced(); fatal_and_redirected();
    struct lazy_counts lazy_after = lazy_counts();
    CHECK(lazy_before.enabled == lazy_after.enabled);
    CHECK(lazy_after.borrowed >= lazy_before.borrowed && lazy_after.reused >= lazy_before.reused &&
          lazy_after.materialized >= lazy_before.materialized);
    if (lazy_after.enabled) {
        CHECK(lazy_after.borrowed >= lazy_before.borrowed + count);
        CHECK(lazy_after.reused >= lazy_before.reused + count);
        CHECK(lazy_after.materialized > lazy_before.materialized);
    }
    uint64_t after = fast_calls();
    if (patch_enabled) CHECK(after >= before && after - before >= count * 4 - 4);
    printf("{\"schema\":\"hd.core-fpstate/1\",\"sse_calls\":%lu,\"avx_calls\":%lu,"
           "\"signal_upper_edit\":true,\"ptrace_upper_edit\":true,\"verified\":true", count * 2, count * 2);
    printf(",\"fp_lazy\":{\"enabled\":%s,\"borrowed\":%" PRIu64 ",\"reused\":%" PRIu64
           ",\"materialized\":%" PRIu64 "}", lazy_after.enabled ? "true" : "false",
           lazy_after.borrowed - lazy_before.borrowed, lazy_after.reused - lazy_before.reused,
           lazy_after.materialized - lazy_before.materialized);
    if (argc == 3) {
        CHECK(sse_seconds > 0 && avx_seconds > 0);
        printf(",\"timing\":{\"threads\":2,\"shared_cpu\":true,\"yield_every\":128,"
               "\"sse_ns_per_call\":%.3f,\"avx_ns_per_call\":%.3f}",
               sse_seconds * 1e9 / (count * 2), avx_seconds * 1e9 / (count * 2));
    }
    puts("}");
    return 0;
}
