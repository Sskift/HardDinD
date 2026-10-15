#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void need(int ok, const char *what) {
    if (!ok) { perror(what); exit(1); }
}
static double now(clockid_t clock) {
    struct timespec ts;
    need(clock_gettime(clock, &ts) == 0, "clock_gettime");
    return ts.tv_sec + ts.tv_nsec / 1e9;
}
static unsigned long long counter(const char *path, const char *key) {
    FILE *f = fopen(path, "r");
    need(f != NULL, path);
    char name[80]; unsigned long long value;
    while (fscanf(f, "%79s %llu", name, &value) == 2) {
        if (!strcmp(name, key)) { fclose(f); return value; }
    }
    fclose(f); fprintf(stderr, "missing counter %s\n", key); exit(1);
}
static void cpu_list(const cpu_set_t *set) {
    int comma = 0;
    putchar('[');
    for (int i = 0; i < CPU_SETSIZE; i++) if (CPU_ISSET(i, set)) {
        printf("%s%d", comma ? "," : "", i); comma = 1;
    }
    putchar(']');
}
static int cpu(int argc, char **argv) {
    double duration = argc > 2 ? atof(argv[2]) : 3;
    need(duration >= 1 && duration <= 10, "duration");
    cpu_set_t allowed, observed;
    CPU_ZERO(&allowed); CPU_ZERO(&observed);
    need(sched_getaffinity(0, sizeof(allowed), &allowed) == 0, "getaffinity");
    if (argc > 3) {
        cpu_set_t requested;
        CPU_ZERO(&requested);
        int lo = -1, hi = -1;
        int fields = sscanf(argv[3], "%d-%d", &lo, &hi);
        if (fields == 1) hi = lo;
        need(fields >= 1 && lo >= 0 && hi >= lo && hi < CPU_SETSIZE, "cpuset argument");
        for (int i = lo; i <= hi; i++) CPU_SET(i, &requested);
        need(CPU_EQUAL(&allowed, &requested), "cpuset differs");
        cpu_set_t all;
        CPU_ZERO(&all);
        for (int i = 0; i < CPU_SETSIZE; i++) CPU_SET(i, &all);
        need(sched_setaffinity(0, sizeof(all), &all) == 0, "setaffinity");
        need(sched_getaffinity(0, sizeof(allowed), &allowed) == 0 && CPU_EQUAL(&allowed, &requested), "escaped cpuset");
    }
    unsigned long long t0 = counter("/sys/fs/cgroup/cpu.stat", "nr_throttled");
    double wall = now(CLOCK_MONOTONIC), used = now(CLOCK_PROCESS_CPUTIME_ID);
    volatile uint64_t value = 1;
    uint64_t iterations = 0;
    do {
        for (int i = 0; i < 100000; i++) value = value * UINT64_C(6364136223846793005) + 1;
        iterations += 100000;
        int current = sched_getcpu();
        need(current >= 0 && CPU_ISSET(current, &allowed), "CPU outside allowed set");
        CPU_SET(current, &observed);
    } while (now(CLOCK_MONOTONIC) - wall < duration);
    used = now(CLOCK_PROCESS_CPUTIME_ID) - used;
    wall = now(CLOCK_MONOTONIC) - wall;
    unsigned long long t1 = counter("/sys/fs/cgroup/cpu.stat", "nr_throttled");
    printf("{\"wall\":%.6f,\"cpu\":%.6f,\"iterations\":%" PRIu64 ",\"checksum\":%" PRIu64 ",\"throttled\":%llu,\"allowed\":",
           wall, used, iterations, value, t1 - t0);
    cpu_list(&allowed); printf(",\"observed\":"); cpu_list(&observed); puts("}");
    return 0;
}
static int pids(void) {
    pid_t children[64]; int count = 0, limited = 0;
    unsigned long long before = counter("/sys/fs/cgroup/pids.events", "max");
    for (int i = 0; i < 64; i++) {
        pid_t p = fork();
        if (p < 0) { limited = errno == EAGAIN; break; }
        if (p == 0) { for (;;) pause(); }
        children[count++] = p;
    }
    for (int i = 0; i < count; i++) {
        need(kill(children[i], SIGKILL) == 0, "kill child");
        int status;
        need(waitpid(children[i], &status, 0) == children[i] && WIFSIGNALED(status), "wait child");
    }
    printf("{\"limited\":%s,\"children\":%d,\"events\":%llu}\n", limited ? "true" : "false", count,
           counter("/sys/fs/cgroup/pids.events", "max") - before);
    return limited ? 0 : 1;
}
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    if (!strcmp(argv[1], "cpu")) return cpu(argc, argv);
    if (!strcmp(argv[1], "pids")) return pids();
    if (!strcmp(argv[1], "oom")) {
        size_t bytes = 256 * 1024 * 1024;
        volatile unsigned char *memory = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        need(memory != MAP_FAILED, "allocate");
        for (size_t i = 0; i < bytes; i += 4096) memory[i] = 0x5a;
        fprintf(stderr, "allocation unexpectedly survived\n"); return 1;
    }
    if (!strcmp(argv[1], "seccomp")) {
        errno = 0;
        need(syscall(SYS_keyctl, 0, 0, 0, 0, 0) == -1 && errno == EPERM, "keyctl not blocked");
        puts("seccomp-denied"); return 0;
    }
    return 2;
}
