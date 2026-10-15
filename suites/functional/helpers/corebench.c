/* Identical static binary runs outside and inside the guest. */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) { perror("clock_gettime"); exit(1); }
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}
static volatile sig_atomic_t signals_received;
static pthread_t receiving_thread;
static void receive_signal(int n) { (void)n; signals_received++; }
static void *send_signals(void *unused) {
    (void)unused;
    struct timespec delay = {.tv_sec=0, .tv_nsec=1000000};
    for (int i=0; i<200; ++i) {
        if (pthread_kill(receiving_thread, SIGUSR1)) return (void *)1;
        nanosleep(&delay, NULL);
    }
    return NULL;
}
static uint64_t expected_compute(void) {
    uint64_t n = 150000000, a = 6364136223846793005ULL, b = 1;
    uint64_t mul = 1, add = 0;
    while (n) {
        if (n & 1) { add = add * a + b; mul *= a; }
        b *= a + 1; a *= a; n >>= 1;
    }
    return mul + add;
}
static void *compute(void *unused) {
    (void)unused;
    volatile uint64_t x = 1;
    for (uint64_t i = 0; i < 150000000; ++i) x = x * 6364136223846793005ULL + 1;
    return (void *)(uintptr_t)x;
}
static int child_status(pid_t pid) {
    int s; pid_t rc;
    do { rc = waitpid(pid, &s, 0); } while (rc < 0 && errno == EINTR);
    return rc != pid || !WIFEXITED(s) || WEXITSTATUS(s) != 0;
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    if (argc < 2) return 2;
    int workers = argc > 2 ? atoi(argv[2]) : 1;
    if (workers < 1 || workers > 64) return 2;
    double t = now();
    if (!strcmp(argv[1], "signals")) {
        struct sigaction sa = {.sa_handler=receive_signal};
        pthread_t sender;
        receiving_thread = pthread_self();
        if (sigaction(SIGUSR1, &sa, NULL) || pthread_create(&sender, NULL, send_signals, NULL)) return 1;
        for (int i=0; i<10000; ++i) if (syscall(SYS_getpid) < 0) return 1;
        void *result;
        if (pthread_join(sender, &result) || result || signals_received == 0) return 1;
    } else if (!strcmp(argv[1], "syscall")) {
        for (int i = 0; i < 100000; ++i)
            if (syscall(SYS_getpid) < 0) return 1;
    } else if (!strcmp(argv[1], "exec")) {
        for (int i = 0; i < 100; ++i) {
            pid_t p = fork();
            if (p < 0) { perror("fork"); return 1; }
            if (!p) { execl(argv[0], argv[0], "child", NULL); _exit(127); }
            if (child_status(p)) return 1;
        }
    } else if (!strcmp(argv[1], "threads")) {
        pthread_t ids[64];
        for (int i = 0; i < workers; ++i) {
            int e = pthread_create(&ids[i], NULL, compute, NULL);
            if (e) { fprintf(stderr, "pthread_create: %s\n", strerror(e)); return 1; }
        }
        for (int i = 0; i < workers; ++i) {
            void *result;
            if (pthread_join(ids[i], &result) || (uint64_t)(uintptr_t)result != expected_compute()) return 1;
        }
    } else if (!strcmp(argv[1], "processes")) {
        pid_t ids[64];
        for (int i = 0; i < workers; ++i) {
            ids[i] = fork();
            if (ids[i] < 0) { perror("fork"); return 1; }
            if (!ids[i]) { _exit((uint64_t)(uintptr_t)compute(NULL) != expected_compute()); }
        }
        for (int i = 0; i < workers; ++i) if (child_status(ids[i])) return 1;
    } else return 2;
    printf("{\"test\":\"%s\",\"workers\":%d,\"seconds\":%.9f,\"verified\":true}\n", argv[1], workers, now()-t);
    return 0;
}
