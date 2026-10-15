/* Diagnostic workload decomposition, never a Docker acceptance test. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

static const char *program;
static long expected_pid;
static volatile sig_atomic_t traps;
static void check(int ok, const char *what) {
    if (!ok) { perror(what); exit(1); }
}
static double now(void) {
    struct timespec t;
    check(clock_gettime(CLOCK_MONOTONIC, &t) == 0, "clock");
    return t.tv_sec + t.tv_nsec * 1e-9;
}
/* Deliberately only a lower-cost reference: no guest kernel, namespaces or
 * interprocess handoff. This handler is NOT a proposed Docker implementation. */
static void trap_getpid(int sig, siginfo_t *info, void *context) {
    (void)sig;
    if (info->si_syscall != SYS_getpid) _exit(90);
    ((ucontext_t *)context)->uc_mcontext.gregs[REG_RAX] = expected_pid;
    traps++;
}
static void install_trap(void) {
    struct sigaction sa = { .sa_sigaction = trap_getpid, .sa_flags = SA_SIGINFO };
    sigemptyset(&sa.sa_mask);
    check(sigaction(SIGSYS, &sa, NULL) == 0, "sigaction");
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog p = { .len = sizeof(filter) / sizeof(filter[0]), .filter = filter };
    check(prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0, "no_new_privs");
    check(prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &p) == 0, "seccomp");
}
static void wait_child(pid_t pid) {
    int status;
    pid_t result;
    do { result = waitpid(pid, &status, 0); } while (result < 0 && errno == EINTR);
    check(result == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0, "child");
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    if (argc != 2 && argc != 3) return 2;
    int small = !strcmp(argv[1], "trace"), trap = argc == 3 && !strcmp(argv[2], "trap");
    if (!small && strcmp(argv[1], "time")) return 2;
    program = argv[0];
    expected_pid = getpid();
    setvbuf(stdout, NULL, _IONBF, 0);
    if (trap) install_trap();
    const char *cases[] = {"empty", "getpid", "map_none", "map_1page", "map_16pages",
                          "map_protect", "reuse_16pages", "protect_pair", "fork", "exec"};
    for (int rep = 0; rep < (small ? 1 : 3); rep++) {
        for (unsigned k = 0; k < sizeof(cases) / sizeof(cases[0]); k++) {
            if (trap && k != 1) continue;
            int n = k == 1 ? (small ? 200 : 100000) : k >= 8 ? (small ? 4 : 40) : (small ? 8 : 128);
            volatile unsigned char *resident = NULL;
            if (k == 6 || k == 7) {
                resident = mmap(NULL, 65536, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                check(resident != MAP_FAILED, "resident map");
                for (int j = 0; j < 16; j++) resident[j * 4096] = 42;
            }
            printf("PROFILE_BEGIN case=%s repeat=%d iterations=%d\n", cases[k], rep, n);
            double start = now();
            for (int i = 0; i < n; i++) {
                if (k == 0) { asm volatile("" ::: "memory"); }
                else if (k == 1) check(syscall(SYS_getpid) == expected_pid, "getpid");
                else if (k >= 2 && k <= 5) {
                    volatile unsigned char *p = mmap(NULL, 65536, PROT_READ | PROT_WRITE,
                                                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                    check(p != MAP_FAILED, "map");
                    int pages = k == 2 ? 0 : k == 3 ? 1 : 16;
                    for (int j = 0; j < pages; j++) p[j * 4096] = (unsigned char)i;
                    if (k == 5) check(mprotect((void *)p, 65536, PROT_READ) == 0, "protect");
                    for (int j = 0; j < pages; j++) check(p[j * 4096] == (unsigned char)i, "data");
                    check(munmap((void *)p, 65536) == 0, "unmap");
                } else if (k == 6) {
                    for (int j = 0; j < 16; j++) resident[j * 4096] = (unsigned char)i;
                    for (int j = 0; j < 16; j++) check(resident[j * 4096] == (unsigned char)i, "resident data");
                } else if (k == 7) {
                    check(mprotect((void *)resident, 65536, PROT_READ) == 0, "read only");
                    check(mprotect((void *)resident, 65536, PROT_READ | PROT_WRITE) == 0, "read write");
                } else {
                    pid_t pid = fork();
                    check(pid >= 0, "fork");
                    if (!pid) {
                        if (k == 9) { execl(program, program, "child", NULL); _exit(91); }
                        _exit(0);
                    }
                    wait_child(pid);
                }
            }
            double seconds = now() - start;
            printf("PROFILE_END {\"case\":\"%s\",\"repeat\":%d,\"iterations\":%d,\"seconds\":%.9f,\"verified\":true}\n",
                   cases[k], rep, n, seconds);
            if (resident) check(munmap((void *)resident, 65536) == 0, "resident unmap");
        }
    }
    if (trap) check(traps == (small ? 200 : 300000), "trap count");
    puts("PROFILE_COMPLETE");
    return 0;
}
