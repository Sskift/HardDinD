/* Targeted rseq lifecycle checks. Full critical-section semantics are tested
 * separately with the pinned kernel's rseq selftests. */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/rseq.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
static cpu_set_t original;
static int cpus[CPU_SETSIZE], ncpus;
static void require(int ok, const char *s) {
    if (!ok) { fprintf(stderr, "%s errno=%d\n", s, errno); _exit(1); }
}
static void check_cpu(int expected) {
    unsigned actual;
    require(__rseq_size >= 8, "libc rseq not registered");
    const volatile struct rseq *r = (const void *)((const char *)__builtin_thread_pointer() + __rseq_offset);
    require(syscall(SYS_getcpu, &actual, NULL, NULL) == 0, "getcpu syscall");
    require(actual == (unsigned)expected && r->cpu_id == actual && r->cpu_id_start == actual && sched_getcpu() == expected, "guest CPU IDs differ");
}
static void cycle(unsigned id) {
    for (int rep = 0; rep < 8; rep++) for (int i = 0; i < ncpus; i++) {
        int cpu = cpus[(i + id) % ncpus]; cpu_set_t one; CPU_ZERO(&one); CPU_SET(cpu, &one);
        require(sched_setaffinity(0, sizeof(one), &one) == 0, "set affinity");
        check_cpu(cpu);
    }
    require(sched_setaffinity(0, sizeof(original), &original) == 0, "restore affinity");
}
static void *worker(void *p) { cycle((unsigned)(uintptr_t)p); return NULL; }
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    require(sched_getaffinity(0, sizeof(original), &original) == 0, "get affinity");
    for (int c = 0; c < CPU_SETSIZE; c++) if (CPU_ISSET(c, &original)) cpus[ncpus++] = c;
    require(ncpus >= 2, "at least two allowed CPUs required");
    if (argc == 2 && !strcmp(argv[1], "exec-child")) {
        cycle(0); puts("{\"event\":\"exec_registration\",\"verified\":true}"); return 0;
    }
    require(argc == 1, "arguments");
    cycle(0);
    pthread_t t[4];
    for (unsigned i = 0; i < 4; i++) require(pthread_create(&t[i], NULL, worker, (void *)(uintptr_t)i) == 0, "pthread_create");
    for (unsigned i = 0; i < 4; i++) require(pthread_join(t[i], NULL) == 0, "pthread_join");
    puts("{\"event\":\"four_thread_migration\",\"verified\":true}");
    pid_t pid = fork(); require(pid >= 0, "fork");
    if (!pid) {
        cycle(1); puts("{\"event\":\"fork_inheritance\",\"verified\":true}");
        execl("/proc/self/exe", argv[0], "exec-child", NULL); _exit(2);
    }
    int status = 0; pid_t got;
    do { got = waitpid(pid, &status, 0); } while (got < 0 && errno == EINTR);
    require(got == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0, "fork/exec child");
    cycle(0); puts("{\"event\":\"parent_after_fork_exec\",\"verified\":true}");
    return 0;
}
