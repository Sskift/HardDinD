/* Bounded host CPU pressure for runner-startup correctness checks. */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static _Atomic int stop;
static _Atomic unsigned int ready;
static uint64_t results[256];

static void *burn(void *arg) {
    size_t id = (size_t)arg;
    uint64_t value = id + 1;
    atomic_fetch_add(&ready, 1);
    while (!atomic_load_explicit(&stop, memory_order_relaxed))
        for (int i = 0; i < 100000; i++) value = value * 6364136223846793005ULL + 1;
    results[id] = value;
    return NULL;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    char *end;
    long seconds = strtol(argv[1], &end, 10);
    if (*end || seconds < 1 || seconds > 60) return 2;
    cpu_set_t cpus;
    if (sched_getaffinity(0, sizeof(cpus), &cpus)) return 1;
    unsigned int count = CPU_COUNT(&cpus);
    if (!count || count > 256) return 1;
    pthread_t threads[256];
    for (size_t i = 0; i < count; i++)
        if (pthread_create(&threads[i], NULL, burn, (void *)i)) return 1;
    while (atomic_load(&ready) != count) sched_yield();
    printf("ready %u\n", count); fflush(stdout);
    struct timespec delay = {.tv_sec = seconds};
    while (nanosleep(&delay, &delay)) {}
    atomic_store(&stop, 1);
    uint64_t checksum = 0;
    for (unsigned int i = 0; i < count; i++) {
        if (pthread_join(threads[i], NULL)) return 1;
        checksum ^= results[i];
    }
    printf("{\"schema\":\"hd.core-pressure/1\",\"threads\":%u,\"checksum\":%llu,\"verified\":true}\n",
           count, (unsigned long long)checksum);
    return 0;
}
