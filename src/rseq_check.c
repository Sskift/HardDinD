// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <linux/filter.h>
#include <linux/rseq.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#define SIGNATURE 0x53053053
extern ptrdiff_t __rseq_offset __attribute__((weak));
extern unsigned int __rseq_size __attribute__((weak));
static __thread struct rseq own_area __attribute__((aligned(32)));
static __thread struct rseq *area;
static __thread int owned;
static cpu_set_t allowed;
static atomic_int entered, gate, ready;
static volatile sig_atomic_t signals, redirected, bad_ip;
static int passed, failed, missing, migration_aborts;
extern const struct rseq_cs hd_rseq_descriptor;

static void result(const char *name, int rc)
{
    passed += !rc; failed += rc == 1; missing += rc == 77;
    printf("{\"check\":\"%s\",\"status\":\"%s\"}\n", name,
           rc == 77 ? "not_tested" : rc ? "fail" : "pass");
    fflush(stdout);
}

static int register_area(void)
{
    if (&__rseq_size && &__rseq_offset && __rseq_size) {
        area = (void *)((char *)__builtin_thread_pointer() + __rseq_offset);
        return (int32_t)area->cpu_id < 0 ? 1 : 0;
    }
    area = &own_area;
    if (syscall(SYS_rseq, area, sizeof(*area), 0, SIGNATURE))
        return errno == ENOSYS || errno == EPERM ? 77 : 1;
    owned = 1;
    return 0;
}

static int unregister_area(void)
{
    return owned && syscall(SYS_rseq, area, sizeof(*area), RSEQ_FLAG_UNREGISTER, SIGNATURE) ? 1 : 0;
}

static int raw_cpu(void)
{
    unsigned int cpu;
    return syscall(SYS_getcpu, &cpu, NULL, NULL) ? -1 : (int)cpu;
}

static int cpu_checks(void)
{
    for (int repeat = 0; repeat < 8; repeat++) for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
        if (!CPU_ISSET(cpu, &allowed)) continue;
        cpu_set_t one;
        CPU_ZERO(&one); CPU_SET(cpu, &one);
        if (sched_setaffinity(0, sizeof(one), &one) || sched_getcpu() != cpu ||
            (area && ((int)area->cpu_id != cpu || (int)area->cpu_id_start != cpu)) ||
            raw_cpu() != cpu) return 1;
    }
    return sched_setaffinity(0, sizeof(allowed), &allowed) ? 1 : 0;
}

/* Keep execution inside the critical section until the peer interrupts it.
   A missing fixup reaches the commit path; correct handling reaches abort. */
static __attribute__((noinline)) int critical(void)
{
    int ret;
    __asm__ volatile (
        ".pushsection __rseq_cs,\"aw\"\n.balign 32\n.globl hd_rseq_descriptor\nhd_rseq_descriptor:\n3:\n"
        ".long 0, 0\n.quad 1f, 2f-1f, 4f\n.popsection\n"
        "leaq 3b(%%rip), %%rax\nmovq %%rax, 8(%[rseq])\n"
        "1: movl $1, (%[entered])\n"
        "6: pause\ncmpl $0, (%[gate])\nje 6b\n"
        "2: xorl %%eax, %%eax\njmp 5f\n"
        ".pushsection __rseq_failure,\"ax\"\n.long 0x53053053\n"
        "4: movl $1, %%eax\njmp 5f\n.popsection\n"
        "5: movq $0, 8(%[rseq])\n"
        : "=&a" (ret) : [rseq] "r" (area), [entered] "r" (&entered), [gate] "r" (&gate)
        : "cc", "memory");
    return ret;
}

static void handle_signal(int sig, siginfo_t *info, void *context)
{
    (void)sig; (void)info;
    ucontext_t *uc = context;
    uintptr_t ip = (uintptr_t)uc->uc_mcontext.gregs[REG_RIP];
    if (ip >= hd_rseq_descriptor.start_ip &&
        ip < hd_rseq_descriptor.start_ip + hd_rseq_descriptor.post_commit_offset)
        bad_ip++;
    if (ip == hd_rseq_descriptor.abort_ip) {
        redirected++;
        if (area->rseq_cs) bad_ip++;
    }
    signals++;
    atomic_store_explicit(&gate, 1, memory_order_relaxed);
}

struct interrupt { pthread_t thread; pid_t tid; int cpu, from_cpu, migrate, acted, error; };
static void *interrupt_thread(void *arg)
{
    struct interrupt *in = arg;
    cpu_set_t other = allowed;
    CPU_CLR(in->from_cpu, &other);
    if (CPU_COUNT(&other) && sched_setaffinity(0, sizeof(other), &other)) {
        in->error = 1; atomic_store(&gate, 1); return NULL;
    }
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);
    atomic_store_explicit(&ready, 1, memory_order_release);
    while (!atomic_load_explicit(&entered, memory_order_acquire)) {
        if (atomic_load_explicit(&gate, memory_order_acquire)) return NULL;
        __asm__ volatile ("pause");
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec - start.tv_sec > 5) { in->error = 1; atomic_store(&gate, 1); return NULL; }
    }
    /* Publication of entered follows installation of the rseq descriptor. */
    in->acted = 1;
    if (in->migrate) {
        cpu_set_t one;
        CPU_ZERO(&one); CPU_SET(in->cpu, &one);
        in->error = sched_setaffinity(in->tid, sizeof(one), &one) != 0;
    } else {
        in->error = pthread_kill(in->thread, SIGUSR1) != 0;
        if (!in->error) return NULL; /* The handler releases the loop. */
    }
    atomic_store_explicit(&gate, 1, memory_order_release);
    return NULL;
}

static int interruption_checks(int migrate)
{
    int cpus[2], n = 0;
    for (int c = 0; c < CPU_SETSIZE && n < 2; c++) if (CPU_ISSET(c, &allowed)) cpus[n++] = c;
    if (migrate && n < 2) return 77;
    struct sigaction sa = {.sa_sigaction = handle_signal, .sa_flags = SA_SIGINFO};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGUSR1, &sa, NULL)) return 1;
    int errors = 0, migrated = 0;
    for (int i = 0; i < 512; i++) {
        if (migrate ? migrated >= 64 : redirected >= 64) break;
        cpu_set_t one;
        CPU_ZERO(&one); CPU_SET(cpus[i % n], &one);
        if (sched_setaffinity(0, sizeof(one), &one)) return 1;
        struct interrupt in = {.thread = pthread_self(), .tid = syscall(SYS_gettid),
                               .cpu = cpus[(i + 1) % n], .from_cpu = cpus[i % n], .migrate = migrate};
        atomic_store(&entered, 0); atomic_store(&gate, 0); atomic_store(&ready, 0);
        pthread_t peer;
        if (pthread_create(&peer, NULL, interrupt_thread, &in)) return 1;
        /* The new thread inherits our one-CPU mask. Let it move away before
           entering rseq, or its startup alone preempts and aborts the test. */
        while (!atomic_load_explicit(&ready, memory_order_acquire) && !atomic_load(&gate)) sched_yield();
        int before = redirected;
        int aborted = critical();
        int seen_cpu = (int)__atomic_load_n(&area->cpu_id, __ATOMIC_RELAXED);
        int seen_start = (int)__atomic_load_n(&area->cpu_id_start, __ATOMIC_RELAXED);
        atomic_store_explicit(&gate, 1, memory_order_release);
        if (pthread_join(peer, NULL)) return 1;
        errors += in.error || (!aborted && ((migrate && in.acted) || redirected > before));
        if (migrate && in.acted) {
            /* Observe the ID before join/getcpu can enter the kernel again.
               An unrelated preemption before migration does not count. */
            migrated += aborted && seen_cpu == in.cpu && seen_start == in.cpu;
            errors += raw_cpu() != in.cpu || (int)area->cpu_id != in.cpu;
        }
    }
    errors += sched_setaffinity(0, sizeof(allowed), &allowed) != 0;
    if (migrate) migration_aborts = migrated;
    return errors || (migrate ? migrated < 64 : redirected < 64 || bad_ip) ? 1 : 0;
}

static void *worker(void *arg)
{
    int *rc = arg;
    *rc = register_area();
    if (!*rc) *rc = cpu_checks() || unregister_area();
    return NULL;
}

static int wait_child(pid_t child, int *exit_status)
{
    int status = 0;
    pid_t got;
    if (child < 0) return 1;
    do got = waitpid(child, &status, 0); while (got < 0 && errno == EINTR);
    if (exit_status) *exit_status = status;
    return got != child || !WIFEXITED(status) || WEXITSTATUS(status) ? 1 : 0;
}

static int trap_free(void)
{
    fflush(stdout);
    pid_t pid = fork();
    if (!pid) {
        struct sock_filter f[] = {
            BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
            BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getcpu, 0, 1),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)
        };
        struct sock_fprog prog = {.len = sizeof(f) / sizeof(f[0]), .filter = f};
        int rc = prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) || prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog);
        _exit(rc || sched_getcpu() < 0 ? 1 : 0);
    }
    return wait_child(pid, NULL);
}

static double latency(int raw)
{
    int calls = 10000;
    double value = 0;
    for (int rep = 0; rep < 2; rep++) {
        struct timespec a, b;
        clock_gettime(CLOCK_MONOTONIC_RAW, &a);
        long long sum = 0;
        for (int i = 0; i < calls; i++) sum += raw ? raw_cpu() : sched_getcpu();
        clock_gettime(CLOCK_MONOTONIC_RAW, &b);
        if (sum < 0) return -1;
        value = ((b.tv_sec - a.tv_sec) * 1e9 + b.tv_nsec - a.tv_nsec) / calls;
        if (value <= 0) return -1;
        calls = (int)(200000000.0 / value);
        if (calls < 1000) calls = 1000;
        if (calls > 20000000) calls = 20000000;
    }
    return value;
}

#ifdef HD_RSEQ_UPSTREAM
#define BINARY(name) extern const unsigned char hd_##name##_start[], hd_##name##_end[]
BINARY(basic_percpu_ops_test);
BINARY(param_test);
BINARY(param_test_compare_twice);
BINARY(syscall_errors_test);
extern char **environ;

static void upstream_one(const char *name, const unsigned char *start,
                         const unsigned char *end, char *const args[])
{
    fflush(stdout);
    struct timespec before, after;
    clock_gettime(CLOCK_MONOTONIC, &before);
    pid_t parent = getpid();
    pid_t pid = fork();
    if (pid < 0) { perror("rseq selftest fork"); result(name, 1); return; }
    if (!pid) {
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != parent) _exit(1);
        alarm(!strcmp(name, "upstream.basic_percpu") ? 240 : 60);
        int fd = syscall(SYS_memfd_create, "rseq-selftest", 0);
        size_t left = (size_t)(end - start);
        if (fd < 0) { perror("rseq selftest memfd"); _exit(1); }
        while (left) {
            ssize_t n = write(fd, start, left);
            if (n <= 0) _exit(1);
            start += n; left -= (size_t)n;
        }
        if (setenv("GLIBC_TUNABLES", "glibc.pthread.rseq=0", 1)) _exit(1);
        fexecve(fd, args, environ);
        perror("rseq selftest exec");
        _exit(1);
    }
    int status = 0;
    int rc = wait_child(pid, &status);
    clock_gettime(CLOCK_MONOTONIC, &after);
    fprintf(stderr, "%s: exit=%d signal=%d seconds=%.3f\n", name,
            WIFEXITED(status) ? WEXITSTATUS(status) : -1,
            WIFSIGNALED(status) ? WTERMSIG(status) : 0,
            (after.tv_sec - before.tv_sec) + (after.tv_nsec - before.tv_nsec) / 1e9);
    result(name, rc);
}

static void upstream_parameters(const char *variant, const unsigned char *start, const unsigned char *end)
{
    const char *modes[] = {"s", "l", "b", "m", "i", "r"};
    for (unsigned int i = 0; i < sizeof(modes) / sizeof(modes[0]); i++) for (int release = 0; release < 2; release++) {
        if (release && modes[i][0] != 'b' && modes[i][0] != 'm') continue;
        for (int delay = 0; delay < 2; delay++) {
            char name[96];
            char *args[16] = {"param_test", "-T", (char *)modes[i], "-r", "100", "-t", "8", "-L"};
            int n = 8;
            if (release) args[n++] = "-M";
            /* Delay immediately before the CPU-ID validation. */
            if (delay) { args[n++] = "-2"; args[n++] = "10000"; }
            args[n] = NULL;
            snprintf(name, sizeof(name), "upstream.param.%s.%s%s%s", variant, modes[i],
                     release ? ".release" : "", delay ? ".interrupt_window" : "");
            upstream_one(name, start, end, args);
        }
    }
}

static void upstream(void)
{
    char *basic[] = {"basic_percpu_ops_test", NULL};
    upstream_one("upstream.basic_percpu", hd_basic_percpu_ops_test_start, hd_basic_percpu_ops_test_end, basic);
    char *errors[] = {"syscall_errors_test", NULL};
    upstream_one("upstream.syscall_errors", hd_syscall_errors_test_start, hd_syscall_errors_test_end, errors);
    upstream_parameters("normal", hd_param_test_start, hd_param_test_end);
    upstream_parameters("compare_twice", hd_param_test_compare_twice_start, hd_param_test_compare_twice_end);
}
#endif

int main(int argc, char **argv)
{
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) return 1;
    int available = register_area();
    if (argc == 2 && !strcmp(argv[1], "--exec-check"))
        return available || cpu_checks() || area->rseq_cs ? 1 : 0;
    int run_upstream = argc == 2 && !strcmp(argv[1], "--upstream");
    int allow_unavailable = argc == 2 && !strcmp(argv[1], "--allow-unavailable");
    if (argc > 1 && !run_upstream && !allow_unavailable) return 2;
    result("rseq.registration", available == 77 && !allow_unavailable ? 1 : available);
    if (available) area = NULL;
    result("sched_getcpu.migration", cpu_checks());
    if (!available) {
        result("rseq.signal_saved_ip", interruption_checks(0));
        result("rseq.migration_abort", interruption_checks(1));
        pthread_t threads[4]; int rc[4] = {0}, started = 0;
        for (; started < 4; started++) if (pthread_create(&threads[started], NULL, worker, &rc[started])) break;
        int errors = started != 4;
        for (int i = 0; i < started; i++) errors |= pthread_join(threads[i], NULL) != 0 || rc[i] != 0;
        result("rseq.threads", errors ? 1 : 0);
        fflush(stdout);
        pid_t pid = fork();
        if (!pid) _exit(cpu_checks() || area->rseq_cs ? 1 : 0);
        result("rseq.fork", wait_child(pid, NULL));
        pid = fork();
        if (!pid) { execl("/proc/self/exe", "rseq_check", "--exec-check", NULL); _exit(1); }
        result("rseq.exec", wait_child(pid, NULL));
        result("sched_getcpu.trap_free", trap_free());
    } else {
        area = NULL;
        result("rseq.critical_sections", 77);
    }
    if (run_upstream) {
#ifdef HD_RSEQ_UPSTREAM
        if (!available) upstream();
        else result("upstream.rseq", 77);
#else
        result("upstream.rseq", 77);
#endif
    }
    double fast = latency(0), slow = latency(1);
    result("sched_getcpu.latency", fast > 0 && slow > 0 ? 0 : 1);
    printf("{\"check\":\"summary\",\"status\":\"%s\",\"pass\":%d,\"fail\":%d,\"not_tested\":%d,"
           "\"rseq_registered\":%s,\"glibc_rseq\":%s,\"signals\":%d,\"signal_aborts\":%d,\"bad_signal_ip\":%d,"
           "\"migration_aborts\":%d,"
           "\"sched_getcpu_ns\":%.3f,\"syscall_getcpu_ns\":%.3f}\n",
           failed ? "fail" : missing ? "not_tested" : "pass", passed, failed, missing,
           available ? "false" : "true", !available && !owned ? "true" : "false",
           signals, redirected, bad_ip, migration_aborts, fast, slow);
    return failed ? 1 : 0;
}
