/* Exercise the exact kernel header, including sleeping/polling races and EINTR. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "stub-futex.h"

struct channel {
    unsigned int word;
    unsigned int payload;
    unsigned int reply;
    unsigned int done;
    unsigned long sleeps[2];
    unsigned long wakes[2];
    unsigned long interrupted[2];
};
static struct channel *channel;
static unsigned int rounds, spins;
static void signal_handler(int sig) { (void)sig; }

static void wait_phase(unsigned int phase, int side) {
    for (unsigned int i = 0; i < spins; ++i) {
        if (stub_futex_phase(&channel->word) != phase) return;
        __asm__ volatile("pause" ::: "memory");
    }
    while (stub_futex_phase(&channel->word) == phase) {
        if (!stub_futex_prepare_wait(&channel->word, phase)) return;
        channel->sleeps[side]++;
        int rc = syscall(SYS_futex, &channel->word, FUTEX_WAIT,
                         phase | STUB_FUTEX_WAITING, NULL, NULL, 0);
        if (rc < 0 && errno == EINTR) channel->interrupted[side]++;
        if (rc < 0 && errno != EINTR && errno != EAGAIN) _exit(3);
    }
}
static void handoff(unsigned int phase, int side) {
    if (stub_futex_handoff(&channel->word, phase)) {
        channel->wakes[side]++;
        int rc;
        do { rc = syscall(SYS_futex, &channel->word, FUTEX_WAKE, 1, NULL, NULL, 0); }
        while (rc < 0 && errno == EINTR);
        if (rc < 0) _exit(4);
    }
}
static void *peer(void *unused) {
    (void)unused;
    for (unsigned int i = 1; i <= rounds; ++i) {
        wait_phase(FUTEX_IN_KERN, 1);
        if (channel->payload != i) _exit(5);
        /* Vary which side enters FUTEX_WAIT first. */
        if (i % 251 == 0) {
            struct timespec delay = {.tv_nsec = 100000};
            nanosleep(&delay, NULL);
        }
        channel->reply = i ^ 0x5a5a5a5aU;
        handoff(FUTEX_IN_KERN, 1);
    }
    return NULL;
}
static void *interruptor(void *arg) {
    pid_t child = (pid_t)(intptr_t)arg;
    while (!__atomic_load_n(&channel->done, __ATOMIC_ACQUIRE)) {
        kill(getpid(), SIGUSR1);
        if (child > 0) kill(child, SIGUSR1);
        struct timespec delay = {.tv_nsec = 50000};
        nanosleep(&delay, NULL);
    }
    return NULL;
}
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    int processes = atoi(argv[1]);
    rounds = strtoul(argv[2], NULL, 10);
    spins = strtoul(argv[3], NULL, 10);
    if (processes < 0 || processes > 1 || !rounds || spins > 10000) return 2;
    channel = mmap(NULL, sizeof(*channel), PROT_READ | PROT_WRITE,
                   MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (channel == MAP_FAILED) return 1;
    channel->word = FUTEX_IN_KERN;
    struct sigaction sa = {.sa_handler = signal_handler};
    if (sigaction(SIGUSR1, &sa, NULL)) return 1;
    /* Default SIGALRM action makes a lost wake a bounded test failure. */
    alarm(30);
    pid_t child = 0;
    pthread_t worker, sender;
    if (processes) {
        child = fork();
        if (child < 0) return 1;
        if (!child) { alarm(30); peer(NULL); _exit(0); }
    } else if (pthread_create(&worker, NULL, peer, NULL)) return 1;
    if (pthread_create(&sender, NULL, interruptor, (void *)(intptr_t)child)) return 1;
    for (unsigned int i = 1; i <= rounds; ++i) {
        channel->payload = i;
        handoff(FUTEX_IN_CHILD, 0);
        wait_phase(FUTEX_IN_CHILD, 0);
        if (channel->reply != (i ^ 0x5a5a5a5aU)) return 6;
    }
    if (processes) {
        int status;
        pid_t rc;
        do { rc = waitpid(child, &status, 0); } while (rc < 0 && errno == EINTR);
        if (rc != child || !WIFEXITED(status) || WEXITSTATUS(status)) return 7;
    } else if (pthread_join(worker, NULL)) return 1;
    __atomic_store_n(&channel->done, 1, __ATOMIC_RELEASE);
    if (pthread_join(sender, NULL)) return 1;
    printf("{\"test\":\"handoff\",\"processes\":%d,\"rounds\":%u,\"spin\":%u,"
           "\"sleeps\":%lu,\"wakes\":%lu,\"interrupted\":%lu,\"verified\":true}\n",
           processes, rounds, spins, channel->sleeps[0] + channel->sleeps[1],
           channel->wakes[0] + channel->wakes[1],
           channel->interrupted[0] + channel->interrupted[1]);
    return 0;
}
