/* Exercise the kernel's exact deferred-IPI owner/pending protocol with real
 * asynchronous signals, interrupt masking windows and four producer queues. */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <ipi-defer.h>

#define ROUNDS 100000
static struct uml_ipi_defer pending;
static volatile sig_atomic_t enabled = 1;
static unsigned long work[4], received, handled[4], batches;
static pthread_t receiver;
static void fail(const char *why) { perror(why); exit(1); }

static void drain(void) {
    if (!enabled || !uml_ipi_defer_pending(&pending) || !uml_ipi_defer_begin(&pending)) return;
    do {
        enabled = 0;
        unsigned int bits = uml_ipi_defer_take(&pending);
        for (unsigned int v = 0; bits; v++, bits >>= 1) {
            if (!(bits & 1)) continue;
            if (enabled || v >= 4) _exit(91);
            __atomic_add_fetch(&handled[v], __atomic_exchange_n(&work[v], 0, __ATOMIC_ACQ_REL), __ATOMIC_RELAXED);
        }
        __atomic_add_fetch(&batches, 1, __ATOMIC_RELAXED);
        enabled = 1;
        asm volatile("" ::: "memory");
    } while (!uml_ipi_defer_end(&pending));
}
static void handler(int sig, siginfo_t *si, void *context) {
    (void)sig; (void)context;
    int saved_errno = errno;
    unsigned int vector = si->si_value.sival_int;
    if (vector >= 4) _exit(92);
    __atomic_add_fetch(&received, 1, __ATOMIC_RELAXED);
    uml_ipi_defer_add(&pending, vector);
    drain();
    errno = saved_errno;
}
static void *producer(void *arg) {
    unsigned int vector = (uintptr_t)arg;
    union sigval value = {.sival_int = vector};
    for (int i = 0; i < ROUNDS; i++) {
        __atomic_add_fetch(&work[vector], 1, __ATOMIC_RELEASE);
        int rc;
        while ((rc = pthread_sigqueue(receiver, SIGRTMIN, value)) == EAGAIN) sched_yield();
        if (rc) { errno = rc; fail("pthread_sigqueue"); }
        /* Bound the backlog so the receiver repeatedly drains and releases
         * ownership, rather than consuming the entire test in one batch. */
        if ((i & 31) == 31) {
            struct timespec delay = {.tv_nsec = 50000};
            while (__atomic_load_n(&handled[vector], __ATOMIC_ACQUIRE) < (unsigned long)i + 1)
                nanosleep(&delay, NULL);
        }
    }
    return NULL;
}
int main(void) {
    struct uml_ipi_defer test = {};
    uml_ipi_defer_add(&test, 2);
    if (!uml_ipi_defer_begin(&test) || uml_ipi_defer_begin(&test) || uml_ipi_defer_take(&test) != 4) return 1;
    /* A notification in the final-release window must retain ownership. */
    uml_ipi_defer_add(&test, 3);
    if (uml_ipi_defer_end(&test) || uml_ipi_defer_take(&test) != 8 || !uml_ipi_defer_end(&test)) return 1;
    uml_ipi_defer_add(&test, 0);
    if (!uml_ipi_defer_begin(&test) || uml_ipi_defer_take(&test) != 1 || !uml_ipi_defer_end(&test)) return 1;
    struct sigaction sa = {.sa_sigaction = handler, .sa_flags = SA_SIGINFO | SA_RESTART};
    sigfillset(&sa.sa_mask);
    if (sigaction(SIGRTMIN, &sa, NULL)) fail("sigaction");
    receiver = pthread_self();
    /* Producers stop after 32 unacknowledged items. Start masked so even a
     * fast host cannot finish all signals before main enters its first window. */
    enabled = 0;
    pthread_t senders[4];
    for (uintptr_t v = 0; v < 4; v++) if (pthread_create(&senders[v], NULL, producer, (void *)v)) fail("create");
    unsigned long windows = 0;
    time_t deadline = time(NULL) + 60;
    while (__atomic_load_n(&received, __ATOMIC_ACQUIRE) < 4UL * ROUNDS) {
        enabled = 0;
        asm volatile("" ::: "memory");
        unsigned long before = __atomic_load_n(&batches, __ATOMIC_RELAXED);
        for (int i = 0; i < 100; i++) asm volatile("pause" ::: "memory");
        if (__atomic_load_n(&batches, __ATOMIC_RELAXED) != before) _exit(93);
        enabled = 1;
        asm volatile("" ::: "memory");
        drain();
        windows++;
        if (time(NULL) > deadline) fail("timeout");
    }
    for (int v = 0; v < 4; v++) if (pthread_join(senders[v], NULL)) fail("join");
    drain();
    int ok = !pending.state && windows && batches >= 100 && received == 4UL * ROUNDS;
    for (int v = 0; v < 4; v++) {
        if (handled[v] != ROUNDS || work[v]) ok = 0;
        if (!ok) fprintf(stderr, "vector=%d handled=%lu pending_work=%lu state=%u windows=%lu batches=%lu\n",
                         v, handled[v], work[v], pending.state, windows, batches);
    }
    printf("{\"test\":\"deferred_ipi\",\"signals\":%lu,\"queue_items\":%d,\"masked_windows\":%lu,\"drain_batches\":%lu,\"verified\":%s}\n",
           received, 4 * ROUNDS, windows, batches, ok ? "true" : "false");
    return !ok;
}
