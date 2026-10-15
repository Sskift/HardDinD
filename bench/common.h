#ifndef HD_BENCH_COMMON_H
#define HD_BENCH_COMMON_H
#define _GNU_SOURCE
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static inline void require(int ok, const char *what) {
    if (!ok) { fprintf(stderr, "%s: %s\n", what, strerror(errno)); exit(1); }
}
static inline double now(void) {
    struct timespec ts;
    require(clock_gettime(CLOCK_MONOTONIC, &ts) == 0, "clock_gettime");
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}
static inline void result(double value, uint64_t verified) {
    require(isfinite(value) && value > 0 && verified > 0, "invalid result");
    printf("{\"value\":%.9g,\"verified\":%llu}\n", value, (unsigned long long)verified);
}
/* Calibrate outside the measurement, then amortize clocks over >=10 ms. */
struct batch_timer {
    uint64_t size, reads, calibration_reads;
    double last, start, elapsed;
    int measuring;
};
static inline struct batch_timer batch_timer(void) {
    struct batch_timer timer = {.size = 1, .last = now()};
    return timer;
}
static inline int batch_next(struct batch_timer *timer, double seconds) {
    double end = now(), batch_seconds = end - timer->last;
    if (timer->measuring) {
        timer->reads++;
        timer->elapsed = end - timer->start;
    } else {
        timer->calibration_reads++;
        if (batch_seconds >= 0.01) { timer->measuring = 1; timer->start = end; }
    }
    if (batch_seconds < 0.01 && timer->size < (UINT64_C(1) << 30)) timer->size *= 2;
    timer->last = end;
    return timer->elapsed < seconds;
}
static inline void batch_result(double value, uint64_t verified, const struct batch_timer *timer) {
    require(isfinite(value) && value > 0 && verified > 0, "invalid result");
    printf("{\"value\":%.9g,\"verified\":%llu,\"batch\":%llu,\"clock_reads\":%llu,"
           "\"calibration_clock_reads\":%llu,\"measurement_seconds\":%.9g}\n", value,
           (unsigned long long)verified, (unsigned long long)timer->size,
           (unsigned long long)timer->reads, (unsigned long long)timer->calibration_reads, timer->elapsed);
}
static inline void write_all(int fd, const void *buffer, size_t size) {
    const char *p = buffer;
    while (size) {
        ssize_t n = write(fd, p, size);
        if (n < 0 && errno == EINTR) continue;
        require(n > 0, "write"); p += n; size -= (size_t)n;
    }
}
static inline void read_all(int fd, void *buffer, size_t size) {
    char *p = buffer;
    while (size) {
        ssize_t n = read(fd, p, size);
        if (n < 0 && errno == EINTR) continue;
        require(n > 0, "read/short input"); p += n; size -= (size_t)n;
    }
}
#endif
