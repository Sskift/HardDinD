/* FP preservation and deliberate signal/ptrace edits across syscall handoffs. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/wait.h>
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

static struct vector pattern(int tag) {
    struct vector v;
    for (unsigned int i = 0; i < sizeof(v.bytes); i++) v.bytes[i] = (unsigned char)(tag + i * 17);
    return v;
}

#define CALL_LOOP \
    "1: mov $39, %%eax; syscall; cmp %[pid], %%rax; je 2f; mov $1, %[bad]; 2:\n" \
    "dec %[n]; jz 3f; test $127, %[n]; jnz 1b; mov $24, %%eax; syscall; jmp 1b; 3:\n"
static void preserve(const struct vector *in, struct vector *out, unsigned long count, int avx) {
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
static void stop_with_vector(int sig, const struct vector *in, struct vector *out) {
    long pid = syscall(SYS_getpid), tid = syscall(SYS_gettid), result;
    __asm__ volatile("vmovdqu %[in], %%ymm15; syscall; vmovdqu %%ymm15, %[out]; vzeroupper"
                     : "=a"(result), [out] "=m"(*out)
                     : "0"(SYS_tgkill), "D"(pid), "S"(tid), "d"((long)sig), [in] "m"(*in)
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
    stop_with_vector(SIGUSR1, &in, &out);
    CHECK(delivered == 1 && !memcmp(&out, &expected, sizeof(out)));
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
int main(int argc, char **argv) {
    CHECK(argc == 2);
    char *end; errno = 0;
    unsigned long count = strtoul(argv[1], &end, 10);
    CHECK(!errno && *argv[1] && !*end && count >= 256 && count <= 1048576);
    if (!__builtin_cpu_supports("avx")) {
        fprintf(stderr, "AVX state unavailable; required checks not tested\n");
        return 77;
    }
    threads(count, 0); threads(count, 1); signals(); traced();
    printf("{\"schema\":\"hd.core-fpstate/1\",\"sse_calls\":%lu,\"avx_calls\":%lu,"
           "\"signal_upper_edit\":true,\"ptrace_upper_edit\":true,\"verified\":true}\n", count * 2, count * 2);
    return 0;
}
