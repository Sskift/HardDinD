/* Compile with the exact os_idle_sleep body extracted from each kernel.
 * Bound poll so the old missed wakeup becomes evidence rather than a hang. */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/signalfd.h>
#include <unistd.h>
#include <ipi-defer.h>

#define IPI_SIGNAL SIGRTMIN
#define panic(...) do { fprintf(stderr, __VA_ARGS__); exit(2); } while (0)
static struct uml_ipi_defer pending;
static int wake_signals, inject, polls, timeouts;
static volatile sig_atomic_t received;
static void handler(int sig) {
    (void)sig;
    received++;
    uml_ipi_defer_add(&pending, 0);
}
static int uml_need_resched(void) {
    /* Deterministic notification inside the check-to-sleep window. */
    if (inject && raise(IPI_SIGNAL)) exit(3);
    return 0;
}
static int timer_alarm_pending(void) { return 0; }
/* The old body does not call this; suppress only that unused warning. */
static __attribute__((unused)) int os_local_ipi_pending(void) {
    return uml_ipi_defer_pending(&pending);
}
static void os_poll(int count, int *fd) {
    if (count != 1) exit(4);
    struct pollfd p = {.fd = *fd, .events = POLLIN};
    polls++;
    int n = poll(&p, 1, 50);
    if (n < 0) exit(5);
    if (!n) timeouts++;
    else if (!(p.revents & POLLIN)) exit(6);
}

#include "idle-under-test.h"

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int corrected = atoi(argv[1]);
    struct sigaction sa = {.sa_handler = handler};
    sigfillset(&sa.sa_mask);
    if (sigaction(IPI_SIGNAL, &sa, NULL)) return 2;
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, IPI_SIGNAL);
    sigaddset(&set, SIGALRM);
    wake_signals = signalfd(-1, &set, SFD_CLOEXEC);
    if (wake_signals < 0) return 2;
    int rounds = corrected ? 1000 : 1;
    for (int i = 0; i < rounds; i++) {
        for (inject = 0; inject <= 1; inject++) {
            pending.state = 0;
            if (!inject && raise(IPI_SIGNAL)) return 3;
            os_idle_sleep();
            if (!uml_ipi_defer_pending(&pending)) return 7;
            sigset_t current;
            if (sigprocmask(SIG_SETMASK, NULL, &current) || sigismember(&current, IPI_SIGNAL)) return 8;
        }
    }
    int ok = received == rounds * 2 && (corrected ? timeouts == 0 && polls == rounds : timeouts == 2);
    printf("{\"corrected\":%s,\"signals\":%d,\"polls\":%d,\"timeouts\":%d,\"verified\":%s}\n",
           corrected ? "true" : "false", received, polls, timeouts, ok ? "true" : "false");
    return !ok;
}
