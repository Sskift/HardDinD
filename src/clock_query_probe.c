/* Diagnostic for the clock/getcpu paths used by cpu_limit_probe. This does
 * not change that workload or establish Docker/performance acceptance.
 * Build once statically and use the identical executable native and guest.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/auxv.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#if defined(__has_include)
# if __has_include(<sys/rseq.h>)
#  include <sys/rseq.h>
#  define HAVE_LIBC_RSEQ_INFO 1
# endif
#endif

#define MAX_THREADS 4
static const char *names[] = {"libc_clock", "syscall_clock", "libc_getcpu", "syscall_getcpu", "libc_clock_getcpu"};
struct worker {
    unsigned id, mode, iterations;
    pthread_barrier_t *barrier;
    cpu_set_t allowed;
    uint64_t start_ns, end_ns, sum, backwards, bad_cpu, completed;
    int error, rseq_cpu_start, rseq_cpu_end;
    unsigned rseq_size;
};
static uint64_t ns(const struct timespec *t) {
    return (uint64_t)t->tv_sec * UINT64_C(1000000000) + (uint64_t)t->tv_nsec;
}
static int stamp(uint64_t *out) {
    struct timespec t;
    if (syscall(SYS_clock_gettime, CLOCK_MONOTONIC_RAW, &t)) return errno;
    *out = ns(&t);
    return 0;
}
static int rseq_cpu(unsigned *size) {
#ifdef HAVE_LIBC_RSEQ_INFO
    *size = __rseq_size;
    if (*size >= 8) {
        const volatile struct rseq *r = (const void *)((const char *)__builtin_thread_pointer() + __rseq_offset);
        return (int)r->cpu_id;
    }
#else
    *size = 0;
#endif
    return -1;
}
static void *run(void *arg) {
    struct worker *w = arg;
    if (sched_getaffinity(0, sizeof(w->allowed), &w->allowed)) w->error = errno;
    w->rseq_cpu_start = rseq_cpu(&w->rseq_size);
    int br = pthread_barrier_wait(w->barrier);
    if (br != 0 && br != PTHREAD_BARRIER_SERIAL_THREAD) w->error = br;
    if (!w->error) w->error = stamp(&w->start_ns);
    uint64_t last = 0;
    for (unsigned i = 0; !w->error && i < w->iterations; i++) {
        if (w->mode == 0 || w->mode == 1 || w->mode == 4) {
            struct timespec t;
            int rc = w->mode == 1 ? (int)syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &t) : clock_gettime(CLOCK_MONOTONIC, &t);
            if (rc) { w->error = errno; break; }
            uint64_t now = ns(&t);
            if (i && now < last) w->backwards++;
            if (t.tv_nsec < 0 || t.tv_nsec >= 1000000000) { w->error = EPROTO; break; }
            last = now;
            w->sum ^= now;
        }
        if (w->mode >= 2) {
            int cpu;
            if (w->mode == 3) {
                unsigned value;
                if (syscall(SYS_getcpu, &value, NULL, NULL)) { w->error = errno; break; }
                cpu = (int)value;
            } else cpu = sched_getcpu();
            if (cpu < 0) { w->error = errno; break; }
            if (cpu >= CPU_SETSIZE || !CPU_ISSET(cpu, &w->allowed)) w->bad_cpu++;
            w->sum += (unsigned)cpu;
        }
        w->completed++;
    }
    int e = stamp(&w->end_ns);
    if (!w->error) w->error = e;
    unsigned ignored;
    w->rseq_cpu_end = rseq_cpu(&ignored);
    return NULL;
}
static unsigned number(const char *s, unsigned max) {
    char *end;
    errno = 0;
    unsigned long n = strtoul(s, &end, 10);
    if (!*s || *end || errno || n < 1 || n > max) { fprintf(stderr, "invalid positive count: %s\n", s); exit(2); }
    return (unsigned)n;
}
int main(int argc, char **argv) {
    if (argc != 4) { fprintf(stderr, "usage: %s THREADS ITERATIONS REPEATS\n", argv[0]); return 2; }
    unsigned threads = number(argv[1], MAX_THREADS), iterations = number(argv[2], 1000000), repeats = number(argv[3], 20);
    setvbuf(stdout, NULL, _IONBF, 0);
    unsigned rs;
    int rcpu = rseq_cpu(&rs);
    printf("{\"event\":\"info\",\"threads\":%u,\"iterations\":%u,\"repeats\":%u,\"vdso_present\":%s,\"libc_rseq_size\":%u,\"libc_rseq_cpu\":%d}\n", threads, iterations, repeats, getauxval(AT_SYSINFO_EHDR) ? "true" : "false", rs, rcpu);
    int failed = 0;
    for (unsigned rep = 0; rep < repeats; rep++) {
        for (unsigned mode = 0; mode < sizeof(names)/sizeof(names[0]); mode++) {
            struct worker w[MAX_THREADS] = {0};
            pthread_t tid[MAX_THREADS];
            pthread_barrier_t barrier;
            int e = pthread_barrier_init(&barrier, NULL, threads);
            if (e) { fprintf(stderr, "barrier: %s\n", strerror(e)); return 1; }
            for (unsigned i = 0; i < threads; i++) {
                w[i].id = i; w[i].mode = mode; w[i].iterations = iterations; w[i].barrier = &barrier;
                e = pthread_create(&tid[i], NULL, run, &w[i]);
                if (e) { fprintf(stderr, "pthread_create: %s\n", strerror(e)); _exit(1); }
            }
            for (unsigned i = 0; i < threads; i++) {
                e = pthread_join(tid[i], NULL);
                if (e) { fprintf(stderr, "pthread_join: %s\n", strerror(e)); return 1; }
                int ok = !w[i].error && !w[i].backwards && !w[i].bad_cpu && w[i].completed == iterations && w[i].end_ns > w[i].start_ns;
                failed |= !ok;
                printf("{\"event\":\"sample\",\"mode\":\"%s\",\"repeat\":%u,\"thread\":%u,\"iterations\":%u,\"completed\":%" PRIu64 ",\"start_raw_ns\":%" PRIu64 ",\"end_raw_ns\":%" PRIu64 ",\"sum\":%" PRIu64 ",\"errno\":%d,\"backwards\":%" PRIu64 ",\"bad_cpu\":%" PRIu64 ",\"rseq_size\":%u,\"rseq_cpu_start\":%d,\"rseq_cpu_end\":%d,\"allowed_cpus\":[", names[mode], rep, i, iterations, w[i].completed, w[i].start_ns, w[i].end_ns, w[i].sum, w[i].error, w[i].backwards, w[i].bad_cpu, w[i].rseq_size, w[i].rseq_cpu_start, w[i].rseq_cpu_end);
                int comma = 0;
                for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) if (CPU_ISSET(cpu, &w[i].allowed)) { printf("%s%d", comma ? "," : "", cpu); comma = 1; }
                printf("],\"verified\":%s}\n", ok ? "true" : "false");
            }
            pthread_barrier_destroy(&barrier);
        }
    }
    printf("{\"event\":\"complete\",\"verified\":%s}\n", failed ? "false" : "true");
    return failed ? 1 : 0;
}
