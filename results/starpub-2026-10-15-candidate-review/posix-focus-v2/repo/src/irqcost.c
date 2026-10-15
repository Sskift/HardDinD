/* Host-only cost reference for the signal mask operation used by UML SMP. */
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(1);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
int main(void) {
    sigset_t set, original, after;
    sigemptyset(&set);
    sigaddset(&set, SIGRTMIN);
    if (sigprocmask(SIG_UNBLOCK, &set, &original)) return 1;
    for (int rep = 0; rep < 6; rep++) {
        double start = now();
        for (int i = 0; i < 100000; i++) {
            if (sigprocmask(SIG_BLOCK, &set, NULL) || sigprocmask(SIG_UNBLOCK, &set, NULL)) return 1;
        }
        double elapsed = now() - start;
        if (sigprocmask(SIG_SETMASK, NULL, &after) || sigismember(&after, SIGRTMIN)) return 1;
        printf("{\"test\":\"host_irq_mask_pair\",\"repeat\":%d,\"iterations\":100000,\"seconds\":%.9f,\"verified\":true}\n", rep, elapsed);
    }
    return sigprocmask(SIG_SETMASK, &original, NULL) != 0;
}
