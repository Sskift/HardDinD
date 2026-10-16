/* Fixed-count syscall probes; timestamps and snapshots stay outside loops. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core syscall failure line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define MAX_VALUES 8192
struct snapshot { unsigned int n; int enabled, patch_enabled, fp_enabled; uint64_t khz; char names[MAX_VALUES][64]; uint64_t values[MAX_VALUES]; };
struct sample { uint64_t ns, operations; struct snapshot first, last; };
struct worker { pthread_barrier_t *barrier; size_t count; int cpu; long pid; int registers; };
static volatile sig_atomic_t trapped;
static long process_id;
#ifdef __x86_64__
extern unsigned char core_syscall_register_site[];
#endif

static uint64_t now(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &ts));
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
static void snapshot_file(struct snapshot *s, const char *path, const char *prefix, int *enabled) {
    FILE *f = fopen(path, "r");
    if (!f) { CHECK(errno == ENOENT); return; }
    char name[64]; uint64_t value;
    while (fscanf(f, "%63s %" SCNu64, name, &value) == 2) {
        if (!strcmp(name, "enabled")) { CHECK(value <= 1); *enabled = (int)value; continue; }
        if (!strcmp(name, "tsc_khz")) { s->khz = value; continue; }
        if (!strcmp(name, "sample_period")) { CHECK(value == 256); continue; }
        CHECK(s->n < MAX_VALUES && strlen(name) == strspn(name, "abcdefghijklmnopqrstuvwxyz0123456789_"));
        int n = snprintf(s->names[s->n], sizeof(s->names[0]), "%s%s", prefix, name);
        CHECK(n > 0 && n < (int)sizeof(s->names[0]));
        s->values[s->n++] = value;
    }
    CHECK(feof(f) && !ferror(f) && !fclose(f));
}
static void snapshot(struct snapshot *s) {
    snapshot_file(s, "/proc/uml_syscall_profile", "", &s->enabled);
    snapshot_file(s, "/proc/uml_syscall_patch", "patch_", &s->patch_enabled);
    snapshot_file(s, "/proc/uml_fp_lazy", "fp_", &s->fp_enabled);
}
static void report(const char *mode, unsigned int threads, const struct sample *s) {
    CHECK(s->ns && s->operations && s->first.n == s->last.n);
    CHECK(s->first.enabled == s->last.enabled && s->first.khz == s->last.khz);
    CHECK(s->first.patch_enabled == s->last.patch_enabled && s->first.fp_enabled == s->last.fp_enabled);
    if (s->last.patch_enabled) {
        int registers = !strcmp(mode, "register_getpid");
        unsigned int i;
        for (i = 0; i < s->first.n; i++)
            if (!strcmp(s->first.names[i], registers ? "patch_dynamic" : "patch_fast_calls")) break;
        CHECK(i < s->first.n && s->last.values[i] >= s->first.values[i]);
        CHECK(s->last.values[i] - s->first.values[i] >= (registers ? s->operations > 1 : s->operations - 1));
#ifdef __x86_64__
        if (registers) CHECK(core_syscall_register_site[0] == 0xff && core_syscall_register_site[3] == 6);
#endif
    }
    printf("{\"mode\":\"%s\",\"threads\":%u,\"operations\":%" PRIu64 ",\"ns_per_op\":%.3f,"
           "\"verified\":true,\"profile_enabled\":%d,\"patch_enabled\":%s,\"tsc_khz\":%" PRIu64 ",\"profile\":{",
           mode, threads, s->operations, (double)s->ns / s->operations, s->last.enabled, s->last.patch_enabled ? "true" : "false", s->last.khz);
    int printed = 0;
    for (unsigned int i = 0; i < s->first.n; i++) {
        CHECK(!strcmp(s->first.names[i], s->last.names[i]) && s->last.values[i] >= s->first.values[i]);
        uint64_t delta = s->last.values[i] - s->first.values[i];
        if (delta) printf("%s\"%s\":%" PRIu64, printed++ ? "," : "", s->first.names[i], delta);
    }
    printf("}}");
}
static void barrier(pthread_barrier_t *b) {
    int result = pthread_barrier_wait(b);
    CHECK(!result || result == PTHREAD_BARRIER_SERIAL_THREAD);
}
static __attribute__((noinline, noclone)) long raw_getpid(void) {
#ifdef __x86_64__
    long result;
    __asm__ volatile(".rept 14; nop; .endr; mov $39, %%eax; syscall"
                     : "=a"(result) : : "rcx", "r11", "memory");
    return result;
#else
    return syscall(SYS_getpid);
#endif
}
static __attribute__((noinline, noclone)) long register_getpid(long nr) {
#ifdef __x86_64__
    long result;
    __asm__ volatile(".rept 14; nop; .endr\n.global core_syscall_register_site\n"
        "core_syscall_register_site: mov %%rsi,%%r10; mov %%rdi,%%rdx; mov %%rcx,%%rsi; mov %%rbx,%%rdi; syscall"
        : "=a"(result) : "0"(nr) : "rcx", "r11", "r10", "rsi", "rdi", "rdx", "memory");
    return result;
#else
    return syscall(nr);
#endif
}
static void *calls(void *arg) {
    struct worker *w = arg;
    cpu_set_t cpus;
    CPU_ZERO(&cpus); CPU_SET(w->cpu, &cpus);
    CHECK(!pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus));
    barrier(w->barrier); barrier(w->barrier);
    if (w->registers)
        for (size_t i = 0; i < w->count; i++) CHECK(register_getpid(SYS_getpid) == w->pid);
    else
        for (size_t i = 0; i < w->count; i++) CHECK(raw_getpid() == w->pid);
    barrier(w->barrier); barrier(w->barrier);
    return NULL;
}
static void parallel(unsigned int threads, size_t count, int registers) {
    struct sample *s = calloc(1, sizeof(*s));
    CHECK(s != NULL);
    pthread_t ids[4]; struct worker w[4]; pthread_barrier_t b;
    int cpus[4], n = 0; cpu_set_t allowed;
    CHECK(!sched_getaffinity(0, sizeof(allowed), &allowed));
    for (int c = 0; c < CPU_SETSIZE && n < 4; c++) if (CPU_ISSET(c, &allowed)) cpus[n++] = c;
    CHECK(n > 0 && !pthread_barrier_init(&b, NULL, threads + 1));
    long pid = syscall(SYS_getpid);
    for (unsigned int i = 0; i < threads; i++) {
        w[i] = (struct worker){&b, count, cpus[i % n], pid, registers};
        CHECK(!pthread_create(&ids[i], NULL, calls, &w[i]));
    }
    barrier(&b); snapshot(&s->first);
    uint64_t started = now();
    barrier(&b); barrier(&b);
    s->ns = now() - started;
    snapshot(&s->last); barrier(&b);
    for (unsigned int i = 0; i < threads; i++) CHECK(!pthread_join(ids[i], NULL));
    CHECK(!pthread_barrier_destroy(&b));
    s->operations = count * threads;
    report(registers ? "register_getpid" : "getpid", threads, s); free(s);
}
static void signal_handler(int sig, siginfo_t *info, void *context) {
#ifdef __x86_64__
    if (sig != SIGSYS || info->si_syscall != SYS_getpid) _exit(90);
    ((ucontext_t *)context)->uc_mcontext.gregs[REG_RAX] = process_id;
    trapped++;
#else
    (void)sig; (void)info; (void)context; _exit(91);
#endif
}
static void signal_cost(size_t count) {
    struct sample *s = mmap(NULL, sizeof(*s), PROT_READ | PROT_WRITE,
                           MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(s != MAP_FAILED);
    memset(s, 0, sizeof(*s));
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        process_id = syscall(SYS_getpid);
        struct sigaction sa = {.sa_sigaction = signal_handler, .sa_flags = SA_SIGINFO};
        sigemptyset(&sa.sa_mask);
        CHECK(!sigaction(SIGSYS, &sa, NULL));
        struct sock_filter code[] = {
            BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
            BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        };
        struct sock_fprog filter = {.len = sizeof(code) / sizeof(code[0]), .filter = code};
        CHECK(!prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0));
        CHECK(!prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter));
        snapshot(&s->first);
        uint64_t started = now();
        for (size_t i = 0; i < count; i++) CHECK(raw_getpid() == process_id);
        s->ns = now() - started;
        CHECK(trapped == (sig_atomic_t)count);
        snapshot(&s->last); s->operations = count;
        _exit(0);
    }
    int status; CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && !WEXITSTATUS(status));
    /* Native measures host SIGSYS+handler+sigreturn; guest measures its own signal path. */
    report("local_sigsys", 1, s);
    CHECK(!munmap(s, sizeof(*s)));
}
static void filter_cost(size_t count, int deny) {
    struct sample *s = mmap(NULL, sizeof(*s), PROT_READ | PROT_WRITE,
                           MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(s != MAP_FAILED);
    memset(s, 0, sizeof(*s));
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        long expected = deny ? -EPERM : syscall(SYS_getpid);
        struct sock_filter code[] = {
            BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
            BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
            BPF_STMT(BPF_RET | BPF_K, deny ? SECCOMP_RET_ERRNO | EPERM : SECCOMP_RET_ALLOW),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        };
        struct sock_fprog filter = {.len = sizeof(code) / sizeof(code[0]), .filter = code};
        CHECK(!prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0));
        CHECK(!prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter));
        snapshot(&s->first);
        uint64_t started = now();
        for (size_t i = 0; i < count; i++) CHECK(raw_getpid() == expected);
        s->ns = now() - started;
        snapshot(&s->last); s->operations = count;
        _exit(0);
    }
    int status; CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && !WEXITSTATUS(status));
    report(deny ? "seccomp_errno" : "seccomp_allow", 1, s);
    CHECK(!munmap(s, sizeof(*s)));
}
static void ptrace_cost(size_t count) {
#ifdef __x86_64__
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        long pid = syscall(SYS_getpid);
        CHECK(!ptrace(PTRACE_TRACEME, 0, NULL, NULL));
        CHECK(!raise(SIGSTOP));
        for (size_t i = 0; i < count; i++) CHECK(raw_getpid() == pid);
        _exit(0);
    }
    int status; CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFSTOPPED(status) && WSTOPSIG(status) == SIGSTOP);
    CHECK(!ptrace(PTRACE_SETOPTIONS, child, NULL, PTRACE_O_TRACESYSGOOD | PTRACE_O_EXITKILL));
    struct sample *s = calloc(1, sizeof(*s)); CHECK(s != NULL);
    snapshot(&s->first);
    uint64_t started = now(), stops = 0;
    for (;;) {
        CHECK(!ptrace(PTRACE_SYSCALL, child, NULL, NULL));
        CHECK(waitpid(child, &status, 0) == child);
        if (WIFEXITED(status)) { CHECK(!WEXITSTATUS(status)); break; }
        CHECK(WIFSTOPPED(status) && WSTOPSIG(status) == (SIGTRAP | 0x80));
        struct user_regs_struct regs;
        CHECK(!ptrace(PTRACE_GETREGS, child, NULL, &regs));
        if (regs.orig_rax == SYS_getpid) {
            if (stops & 1) CHECK(regs.rax == (unsigned long long)child);
            stops++;
        } else CHECK(regs.orig_rax == SYS_exit_group && stops == count * 2);
    }
    s->ns = now() - started;
    snapshot(&s->last); s->operations = count;
    CHECK(stops == count * 2);
    report("ptraced_getpid", 1, s); free(s);
#else
    (void)count; CHECK(0);
#endif
}
int main(int argc, char **argv) {
    CHECK(argc == 3);
    char *end; errno = 0;
    unsigned long count = strtoul(argv[2], &end, 10);
    CHECK(!errno && *argv[2] && !*end && count > 0 && count <= 1048576);
    int all = !strcmp(argv[1], "all");
    int registers = !strcmp(argv[1], "registers");
    CHECK(all || registers || !strcmp(argv[1], "threads") || !strcmp(argv[1], "signals") ||
          !strcmp(argv[1], "filters") || !strcmp(argv[1], "ptrace"));
    struct snapshot initial = {0};
    snapshot(&initial);
    unsigned int hot = 0;
    for (unsigned int i = 0; i < initial.n; i++)
        if (!strcmp(initial.names[i], "patch_hot_threshold")) hot = (unsigned int)initial.values[i];
    if (initial.patch_enabled && hot) {
        long pid = syscall(SYS_getpid);
        for (unsigned int i = 0; i < hot; i++) CHECK(raw_getpid() == pid);
        for (unsigned int i = 0; i < hot; i++) CHECK(register_getpid(SYS_getpid) == pid);
    }
    printf("{\"schema\":\"hd.core-syscalls/1\",\"samples\":[");
    if (all || registers || !strcmp(argv[1], "threads"))
        for (unsigned int n = 1; n <= 4; n++) { if (n > 1) putchar(','); parallel(n, count, registers); }
    if (all || !strcmp(argv[1], "signals")) { if (all) putchar(','); signal_cost(count); }
    if (all || !strcmp(argv[1], "filters")) {
        if (all) putchar(',');
        filter_cost(count, 0); putchar(','); filter_cost(count, 1);
    }
    if (all || !strcmp(argv[1], "ptrace")) { if (all) putchar(','); ptrace_cost(count); }
    puts("]}");
    return 0;
}
