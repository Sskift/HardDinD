/* cpu_capture_witness v2 - round12 diagnostic ONLY, separate from the frozen
 * main cpu_limit_probe/checker. Supersedes rejected-v1/cpu_capture_witness.c
 * (owner review OWNER-CORRELATOR-REVIEW.md):
 *   - every capture preserves the raw cgroup evidence it read (both cpu.max
 *     values with lengths/errno, cpu.stat counters + length, /proc/stat line
 *     length/errno), so same-window throttle state never has to be inferred;
 *   - sync events are bracketed: MONOTONIC before/after the REALTIME read,
 *     giving the host correlator an explicit offset interval, not a point;
 *   - completion is explicit: requested/completed counts + status; deadline
 *     truncation yields status "deadline" and exit code 3 (never silent 0);
 *   - numeric arguments parsed strictly; clock_nanosleep errors other than
 *     EINTR abort with status "clock_error".
 *
 *   cpu-capture-witness witness [--count 1..2000] [--interval-ms 20..5000]
 *
 * Persistent-fd lseek+read stages, no worker threads (external load is the
 * owner's). Read-only: never writes cgroup files, never changes affinity.
 * Bounded by count and an absolute wall deadline. Static, musl/glibc
 * independent. Raw JSONL; all analysis lives offline.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define MAX_COUNT 2000
#define MAX_INTERVAL_MS 5000
#define DEADLINE_GRACE_NS 3000000000ull

struct held_fd { char path[256]; int fd; };

static uint64_t now_ns(clockid_t clk)
{
    struct timespec ts;
    if (clock_gettime(clk, &ts)) { perror("clock_gettime"); exit(1); }
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int parse_count(const char *s, int *out)
{
    char *end = NULL;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || !end || *end != '\0' || v < 1 || v > MAX_COUNT) return -1;
    *out = (int)v;
    return 0;
}

static int parse_interval(const char *s, uint64_t *out)
{
    char *end = NULL;
    errno = 0;
    unsigned long long v = strtoull(s, &end, 10);
    if (errno || !end || *end != '\0' || v < 20 || v > MAX_INTERVAL_MS) return -1;
    *out = (uint64_t)v;
    return 0;
}

static void held_open(struct held_fd *h)
{
    h->fd = open(h->path, O_RDONLY);   /* errno retained for the caller */
}

/* Returns bytes read (not counting the NUL), or -1 with errno. */
static ssize_t held_read(struct held_fd *h, char *buf, size_t cap)
{
    if (h->fd < 0) {
        held_open(h);
        if (h->fd < 0) return -1;
    }
    if (lseek(h->fd, 0, SEEK_SET) < 0) return -1;
    size_t n = 0;
    for (;;) {
        ssize_t got = read(h->fd, buf + n, cap - 1 - n);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) return -1;
        if (!got) break;
        n += (size_t)got;
        if (n == cap - 1) break;         /* truncated: flagged via length */
    }
    buf[n] = 0;
    return (ssize_t)n;
}

static ssize_t held_read_line(struct held_fd *h, char *buf, size_t cap)
{
    if (h->fd < 0) {
        held_open(h);
        if (h->fd < 0) return -1;
    }
    if (lseek(h->fd, 0, SEEK_SET) < 0) return -1;
    size_t n = 0;
    while (n < cap - 1) {
        ssize_t got = read(h->fd, buf + n, cap - 1 - n);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) return -1;
        if (!got) break;
        n += (size_t)got;
        if (memchr(buf, '\n', n)) break;
    }
    buf[n] = 0;
    return (ssize_t)n;
}

static uint64_t field_u64(const char *text, const char *key, int *found)
{
    size_t klen = strlen(key);
    for (const char *p = text; (p = strstr(p, key)) != NULL; p++) {
        if ((p == text || p[-1] == '\n') && p[klen] == ' ') {
            char *end = NULL;
            errno = 0;
            unsigned long long v = strtoull(p + klen + 1, &end, 10);
            if (!errno && (*end == '\n' || *end == '\0')) { *found = 1; return v; }
        }
    }
    *found = 0;
    return 0;
}

struct cgstat {
    int present;
    uint64_t usage_usec, nr_periods, nr_throttled, throttled_usec;
};

static struct cgstat parse_stat(const char *text)
{
    struct cgstat s;
    memset(&s, 0, sizeof(s));
    int a = 0, b = 0, c = 0, d = 0;
    s.usage_usec = field_u64(text, "usage_usec", &a);
    s.nr_periods = field_u64(text, "nr_periods", &b);
    s.nr_throttled = field_u64(text, "nr_throttled", &c);
    s.throttled_usec = field_u64(text, "throttled_usec", &d);
    s.present = (a && b && c && d);
    return s;
}

struct stage {
    uint64_t begin, end, tcpu0, tcpu1;
};

#define NSTAGE 5
static const char *const names[NSTAGE] = {
    "cpu.max.first", "cpu.stat", "cpu.max.second", "/proc/stat", "process_clock"
};

static void emit_sync(const char *tag)
{
    /* Bracketed exchange: the REALTIME read lies between the two MONOTONIC
       reads, which bounds the cross-clock pairing for the correlator. */
    uint64_t mb = now_ns(CLOCK_MONOTONIC);
    uint64_t rt = now_ns(CLOCK_REALTIME);
    uint64_t ma = now_ns(CLOCK_MONOTONIC);
    printf("{\"event\":\"sync\",\"tag\":\"%s\",\"rt_ns\":%llu,"
           "\"mono_before_ns\":%llu,\"mono_after_ns\":%llu,\"pid\":%ld,"
           "\"tid\":%ld,\"clk_tck\":%ld}\n",
           tag, (unsigned long long)rt, (unsigned long long)mb,
           (unsigned long long)ma, (long)getpid(), (long)syscall(SYS_gettid),
           (long)sysconf(_SC_CLK_TCK));
    fflush(stdout);
}

/* Returns 0 on schedule, -1 on a non-EINTR clock error. */
static int sleep_until(uint64_t mono_deadline)
{
    struct timespec ts = { (time_t)(mono_deadline / 1000000000ull),
                           (long)(mono_deadline % 1000000000ull) };
    for (;;) {
        int e = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
        if (e == 0) return 0;
        if (e != EINTR) { errno = e; return -1; }
    }
}

int main(int argc, char **argv)
{
    int count = 100, rc = 0;
    uint64_t interval_ms = 100;
    int i = 1;
    if (argc >= 2 && !strcmp(argv[1], "witness")) i = 2;
    for (; i < argc; i++) {
        if (!strcmp(argv[i], "--count") && i + 1 < argc) {
            if (parse_count(argv[++i], &count)) { fprintf(stderr, "bad --count\n"); return 2; }
        } else if (!strcmp(argv[i], "--interval-ms") && i + 1 < argc) {
            if (parse_interval(argv[++i], &interval_ms)) { fprintf(stderr, "bad --interval-ms\n"); return 2; }
        } else {
            fprintf(stderr, "usage: witness [--count 1..%d] [--interval-ms 20..%d]\n",
                    MAX_COUNT, MAX_INTERVAL_MS);
            return 2;
        }
    }

    struct held_fd fmax = {{0}, -1}, fstat = {{0}, -1}, fproc = {{0}, -1};
    snprintf(fmax.path, sizeof(fmax.path), "/sys/fs/cgroup/%s", "cpu.max");
    snprintf(fstat.path, sizeof(fstat.path), "/sys/fs/cgroup/%s", "cpu.stat");
    snprintf(fproc.path, sizeof(fproc.path), "%s", "/proc/stat");
    held_open(&fmax);
    held_open(&fstat);
    held_open(&fproc);
    char mb[128], m2[128], sb[2048], pb[256];
    if (fmax.fd < 0 || held_read(&fmax, mb, sizeof(mb)) < 0 ||
        fstat.fd < 0 || held_read(&fstat, sb, sizeof(sb)) < 0 ||
        fproc.fd < 0 || held_read_line(&fproc, pb, sizeof(pb)) < 0) {
        fprintf(stderr, "required files unreadable: %s\n", strerror(errno));
        return 1;
    }

    uint64_t start = now_ns(CLOCK_MONOTONIC);
    uint64_t deadline = start + (uint64_t)count * interval_ms * 1000000ull
                        + DEADLINE_GRACE_NS;
    const char *status = "complete";
    int completed = 0;
    emit_sync("start");

    for (int k = 0; k < count; k++) {
        if (k && sleep_until(start + (uint64_t)k * interval_ms * 1000000ull)) {
            status = "clock_error";
            rc = 1;
            break;
        }
        struct stage st[NSTAGE];
        memset(st, 0, sizeof(st));
        ssize_t len1 = -1, len2 = -1, lens = -1, lenp = -1;
        int e1 = 0, e2 = 0, es = 0, ep = 0;

        st[0].begin = now_ns(CLOCK_MONOTONIC);
        st[0].tcpu0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        len1 = held_read(&fmax, mb, sizeof(mb));
        if (len1 < 0) { e1 = errno; mb[0] = 0; }
        st[0].tcpu1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        st[0].end = now_ns(CLOCK_MONOTONIC);

        st[1].begin = now_ns(CLOCK_MONOTONIC);
        st[1].tcpu0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        lens = held_read(&fstat, sb, sizeof(sb));
        if (lens < 0) { es = errno; sb[0] = 0; }
        st[1].tcpu1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        st[1].end = now_ns(CLOCK_MONOTONIC);

        st[2].begin = now_ns(CLOCK_MONOTONIC);
        st[2].tcpu0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        len2 = held_read(&fmax, m2, sizeof(m2));
        if (len2 < 0) { e2 = errno; m2[0] = 0; }
        st[2].tcpu1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        st[2].end = now_ns(CLOCK_MONOTONIC);

        st[3].begin = now_ns(CLOCK_MONOTONIC);
        st[3].tcpu0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        lenp = held_read_line(&fproc, pb, sizeof(pb));
        if (lenp < 0) ep = errno;
        st[3].tcpu1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        st[3].end = now_ns(CLOCK_MONOTONIC);

        st[4].begin = now_ns(CLOCK_MONOTONIC);
        st[4].tcpu0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        uint64_t pcpu = now_ns(CLOCK_PROCESS_CPUTIME_ID);
        st[4].tcpu1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
        st[4].end = now_ns(CLOCK_MONOTONIC);

        /* cpu.max is a single line; trim CR/LF so the emitted raw is one
           JSON line (the recorded len is the untrimmed read length). */
        for (char *p = mb + strlen(mb); p > mb && (p[-1] == '\n' || p[-1] == '\r'); p--) p[-1] = 0;
        for (char *p = m2 + strlen(m2); p > m2 && (p[-1] == '\n' || p[-1] == '\r'); p--) p[-1] = 0;
        struct cgstat cg = parse_stat(sb);
        int stable = (!e1 && !e2 && !strcmp(mb, m2));

        printf("{\"event\":\"cap\",\"k\":%d,\"mono_begin_ns\":%llu,"
               "\"proc_cpu_ns\":%llu,\"max_stable\":%d,\"stages\":[",
               k, (unsigned long long)st[0].begin,
               (unsigned long long)pcpu, stable);
        for (int i = 0; i < NSTAGE; i++) {
            printf("%s{\"name\":\"%s\",\"begin_ns\":%llu,\"end_ns\":%llu,"
                   "\"tcpu_begin_ns\":%llu,\"tcpu_end_ns\":%llu}",
                   i ? "," : "", names[i],
                   (unsigned long long)st[i].begin, (unsigned long long)st[i].end,
                   (unsigned long long)st[i].tcpu0, (unsigned long long)st[i].tcpu1);
        }
        printf("],\"max1\":{\"errno\":%d,\"len\":%zd,\"raw\":\"%s\"},"
               "\"stat\":{\"errno\":%d,\"len\":%zd,\"present\":%d,"
               "\"usage_usec\":%llu,\"nr_periods\":%llu,\"nr_throttled\":%llu,"
               "\"throttled_usec\":%llu},\"max2\":{\"errno\":%d,\"len\":%zd,"
               "\"raw\":\"%s\"},\"proc\":{\"errno\":%d,\"len\":%zd}}\n",
               e1, len1, mb,
               es, lens, cg.present,
               (unsigned long long)cg.usage_usec,
               (unsigned long long)cg.nr_periods,
               (unsigned long long)cg.nr_throttled,
               (unsigned long long)cg.throttled_usec,
               e2, len2, m2, ep, lenp);
        fflush(stdout);
        completed = k + 1;
        if (now_ns(CLOCK_MONOTONIC) > deadline) {
            status = "deadline";
            rc = 3;
            break;
        }
    }
    uint64_t end = now_ns(CLOCK_MONOTONIC);
    emit_sync("end");
    fprintf(stderr, "{\"event\":\"summary\",\"requested\":%d,\"completed\":%d,"
                    "\"status\":\"%s\",\"start_mono_ns\":%llu,"
                    "\"end_mono_ns\":%llu,\"deadline_ns\":%llu}\n",
            count, completed, status, (unsigned long long)start,
            (unsigned long long)end, (unsigned long long)deadline);
    return rc;
}
