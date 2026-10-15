/* Probe real host namespace creation; every attempt runs in a disposable child. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/sched.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static int attempt(int operation, unsigned long flags) {
    if (operation == 0) return unshare(flags) == 0 ? 0 : errno;
    long pid;
    if (operation == 1) {
        pid = syscall(SYS_clone, flags | SIGCHLD, 0, 0, 0, 0);
    } else {
        struct clone_args args = {.flags = flags, .exit_signal = SIGCHLD};
        pid = syscall(SYS_clone3, &args, sizeof(args));
    }
    if (pid < 0) return errno;
    if (!pid) _exit(0);
    int status;
    return waitpid(pid, &status, 0) == pid && WIFEXITED(status) && !WEXITSTATUS(status) ? 0 : ECHILD;
}

int main(void) {
#if !defined(__x86_64__)
    fputs("namespace probe currently requires x86_64\n", stderr);
    return 2;
#endif
    const struct { const char *name; unsigned long flags; } cases[] = {
        {"plain", 0}, {"user", CLONE_NEWUSER}, {"mount", CLONE_NEWNS},
        {"network", CLONE_NEWNET}, {"pid", CLONE_NEWPID}, {"cgroup", CLONE_NEWCGROUP},
        {"user_mount", CLONE_NEWUSER | CLONE_NEWNS},
        {"user_network", CLONE_NEWUSER | CLONE_NEWNET},
    };
    const char *operations[] = {"unshare", "clone", "clone3"};
    for (int op = 0; op < 3; ++op) {
        for (unsigned int i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
            int pipefd[2];
            if (pipe(pipefd)) return 1;
            pid_t child = fork();
            if (child < 0) return 1;
            if (!child) {
                close(pipefd[0]);
                alarm(5);
                int error = attempt(op, cases[i].flags);
                if (write(pipefd[1], &error, sizeof(error)) != sizeof(error)) _exit(1);
                _exit(0);
            }
            close(pipefd[1]);
            int error, status;
            ssize_t n = read(pipefd[0], &error, sizeof(error));
            close(pipefd[0]);
            if (waitpid(child, &status, 0) != child || n != sizeof(error) ||
                !WIFEXITED(status) || WEXITSTATUS(status)) return 1;
            printf("{\"syscall\":\"%s\",\"namespace\":\"%s\",\"errno\":%d,\"allowed\":%s}\n",
                   operations[op], cases[i].name, error, error ? "false" : "true");
        }
    }
    return 0; /* Denial is a measured result, not an incomplete probe. */
}
