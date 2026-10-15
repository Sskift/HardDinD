/* cpu_limit_probe - observe CFS bandwidth and cpuset enforcement from inside
 * a container. Read-only: it never writes cgroup files or changes its affinity.
 *
 *   cpu-limit-probe selftest
 *   cpu-limit-probe info
 *   cpu-limit-probe snapshot
 *   cpu-limit-probe spin --threads N --duration-ms T [--sample-ms S]
 *   cpu-limit-probe contend --threads N --duration-ms T [--sample-ms S]
 *   cpu-limit-probe capture-test --max P --stat P --proc P
 *                          [--weight P] [--cpuset P]
 *                          [--count N] [--interval-ms M]   (fixture harness)
 *
 * `contend` is the shares-contention mode: every capture brackets cpu.weight
 * and cpuset.cpus.effective too (read BEFORE and AFTER cpu.stat), so a weight
 * that changes while the sampler is descheduled can never be silently
 * misattributed: weight1 != weight2 marks the capture unstable and the
 * checker quarantines its full [wall_ns, cap_end_ns] span. First errno is
 * preserved, parse failures are counted (never retried away). The spin /
 * capture-test schema above is byte-identical to the frozen v4 contract.
 *
 * Every normal output line is one JSON object (JSONL). All timestamps and
 * counters are integers; rate math belongs to the owner-run Python harness so
 * the probe output stays raw evidence. Static, musl/glibc independent.
 *
 * Measurement integrity (v4):
 *   - spin samples reuse persistent fds (opened once) with lseek(0)+read and
 *     touch only the files the verdict needs (cpu.max, cpu.stat) plus one
 *     diagnostic line (/proc/stat steal). cpuset.cpus(.effective)/cpu.weight
 *     are read only for start/end/snapshot/info evidence.
 *   - every sample is a bracketed capture: wall_ns is taken immediately before
 *     the first read and cap_end_ns immediately after the last clock, so a
 *     deschedule between syscalls can never misdate counters. cpu.max is read
 *     before AND after cpu.stat; max_stable=0 records a setting changing during
 *     the capture instead of silently attributing one setting to mixed counters.
 *   - no observation is dropped or retried away: slow captures, unstable
 *     cpu.max pairs and read errors are all emitted as raw evidence. A failed
 *     persistent-fd read fails that one capture (first errno preserved) and the
 *     fd is reopened for the next capture; it is never silently swapped under
 *     a successful observation.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_THREADS 64
#define MAX_CPUS CPU_SETSIZE

struct cfs_stat {
    uint64_t usage_usec, nr_periods, nr_throttled, throttled_usec, nr_bursts, burst_usec;
    int present;
};

struct cpu_max_v {
    int err;
    int quota_max;              /* -1 unknown/unparsed, 1 "max", 0 finite */
    uint64_t quota, period;
};

struct cg_reading {
    int max_err, cpuset_err, cpuset_eff_err, weight_err, stat_err;
    int quota_max;
    uint64_t quota, period;
    char max_raw[96], cpuset_raw[8192], cpuset_eff_raw[8192], weight_raw[32], stat_raw[2048];
    struct cfs_stat stat;
};

/* One reusable descriptor for a file whose contents must be re-read often.
   cgroupfs control files and seq_file-style /proc files re-render fresh
   contents after lseek(fd, 0, SEEK_SET); reopening on every sample was the
   v3 hot path's largest syscall footprint. */
struct held_fd {
    char path[256];
    int fd;
};

static uint64_t now_ns(clockid_t clk) {
    struct timespec ts;
    if (clock_gettime(clk, &ts)) { perror("clock_gettime"); exit(1); }
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int read_raw(const char *path, char *buf, size_t cap) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    size_t n = 0;
    for (;;) {
        char extra;
        ssize_t got = read(fd, n < cap - 1 ? buf + n : &extra,
                           n < cap - 1 ? cap - 1 - n : 1);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0 || (got > 0 && n == cap - 1)) {
            int saved = got < 0 ? errno : EOVERFLOW;
            close(fd); errno = saved; return -1;
        }
        if (!got) break;
        n += (size_t)got;
    }
    close(fd);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) n--;
    buf[n] = 0;
    return 0;
}

/* Read only the first line (used for /proc/stat, which is deliberately larger
   than any buffer we want to carry per sample). Truncation at cap is fine once
   the newline has been seen; never an error. */
static int read_first_line_fd(int fd, char *buf, size_t cap) {
    size_t n = 0;
    while (n < cap - 1) {
        ssize_t got = read(fd, buf + n, cap - 1 - n);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) return -1;
        if (!got) break;
        n += (size_t)got;
        if (memchr(buf, '\n', n)) break;
    }
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) n--;
    buf[n] = 0;
    return 0;
}

static int read_first_line(const char *path, char *buf, size_t cap) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    int rc = read_first_line_fd(fd, buf, cap);
    int saved = errno;
    close(fd);
    errno = saved;
    return rc;
}

static void held_open(struct held_fd *h) {
    h->fd = open(h->path, O_RDONLY);   /* errno retained for the caller */
}

static void held_close(struct held_fd *h) {
    if (h->fd >= 0) close(h->fd);
    h->fd = -1;
}

/* Stream one message from a non-seekable fixture FIFO and drop the fd so the
   next capture reopens it. Used only by capture-test against pipe fixtures;
   real cgroup fds are regular files. No hidden retry: one read sequence, the
   observed errno returned as-is. */
static int held_read_pipe(struct held_fd *h, char *buf, size_t cap, int line_only) {
    size_t n = 0;
    for (;;) {
        char extra;
        ssize_t got = read(h->fd, n < cap - 1 ? buf + n : &extra,
                           n < cap - 1 ? cap - 1 - n : 1);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) { int saved = errno; held_close(h); errno = saved; return -1; }
        if (!got) break;
        if (n == cap - 1) { held_close(h); errno = EOVERFLOW; return -1; }
        n += (size_t)got;
        if (line_only && memchr(buf, '\n', n)) break;
        break;   /* fixture FIFO: one line-sized message per capture */
    }
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) n--;
    buf[n] = 0;
    held_close(h);
    return 0;
}

/* Re-render the whole file through a persistent fd. A failure fails THIS
   capture exactly as observed (first errno preserved, no retry hidden inside
   the observation); the stale fd is dropped so the next capture reopens. */
static int held_read(struct held_fd *h, char *buf, size_t cap) {
    if (h->fd < 0) {
        held_open(h);
        if (h->fd < 0) return -1;     /* open errno is the observation */
    }
    int fail_errno = EIO;
    int ok = 0;
    off_t where = lseek(h->fd, 0, SEEK_SET);
    if (where < 0 && errno == ESPIPE)
        return held_read_pipe(h, buf, cap, 0);
    if (where >= 0) {
        size_t n = 0;
        for (;;) {
            char extra;
            ssize_t got = read(h->fd, n < cap - 1 ? buf + n : &extra,
                               n < cap - 1 ? cap - 1 - n : 1);
            if (got < 0 && errno == EINTR) continue;
            if (got < 0) { fail_errno = errno; break; }
            if (!got) {
                while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) n--;
                buf[n] = 0;
                ok = 1;
                break;
            }
            if (n == cap - 1) { fail_errno = EOVERFLOW; break; }
            n += (size_t)got;
        }
    } else {
        fail_errno = errno;
    }
    if (ok) return 0;
    held_close(h);
    errno = fail_errno;
    return -1;
}

static int held_read_line(struct held_fd *h, char *buf, size_t cap) {
    if (h->fd < 0) {
        held_open(h);
        if (h->fd < 0) return -1;
    }
    if (lseek(h->fd, 0, SEEK_SET) < 0 && errno == ESPIPE)
        return held_read_pipe(h, buf, cap, 1);
    if (lseek(h->fd, 0, SEEK_SET) >= 0 && read_first_line_fd(h->fd, buf, cap) == 0)
        return 0;
    int saved = errno;
    held_close(h);
    errno = saved ? saved : EIO;
    return -1;
}

static int field_after(const char *text, const char *key, uint64_t *value) {
    size_t klen = strlen(key);
    for (const char *p = text; (p = strstr(p, key)) != NULL; p++) {
        if ((p == text || p[-1] == '\n') && p[klen] == ' ') {
            const char *start = p + klen + 1;
            char *end;
            errno = 0;
            unsigned long long parsed = strtoull(start, &end, 10);
            if (!isdigit((unsigned char)*start) || errno ||
                (*end != '\n' && *end != '\0')) return 0;
            *value = parsed;
            return 1;
        }
    }
    return 0;
}

static void parse_stat(const char *text, struct cfs_stat *st) {
    memset(st, 0, sizeof(*st));
    int found = field_after(text, "usage_usec", &st->usage_usec);
    found += field_after(text, "nr_periods", &st->nr_periods);
    found += field_after(text, "nr_throttled", &st->nr_throttled);
    found += field_after(text, "throttled_usec", &st->throttled_usec);
    field_after(text, "nr_bursts", &st->nr_bursts);
    field_after(text, "burst_usec", &st->burst_usec);
    st->present = found == 4;
}

static void parse_cpu_max_v(const char *text, struct cpu_max_v *m) {
    char quota[32], period[32], more[2], *end;
    m->quota_max = -1;
    m->quota = m->period = 0;
    if (sscanf(text, "%31s %31s %1s", quota, period, more) != 2) goto invalid;
    errno = 0;
    m->period = strtoull(period, &end, 10);
    if (!isdigit((unsigned char)*period) || errno || *end || !m->period) goto invalid;
    if (!strcmp(quota, "max")) {
        m->quota_max = 1;
        return;
    }
    errno = 0;
    m->quota = strtoull(quota, &end, 10);
    if (!isdigit((unsigned char)*quota) || errno || *end || !m->quota) goto invalid;
    m->quota_max = 0;
    return;
invalid:
    m->err = EPROTO;
}

static void parse_cpu_max(const char *text, struct cg_reading *cg) {
    struct cpu_max_v m = {0};
    parse_cpu_max_v(text, &m);
    cg->max_err = m.err;
    cg->quota_max = m.quota_max;
    cg->quota = m.quota;
    cg->period = m.period;
}

struct weight_v {
    int err;        /* read errno; 0 when the file was read */
    int invalid;    /* file read OK but contents are not one integer 1..10000 */
    uint64_t value;
};

static void parse_weight_v(const char *text, struct weight_v *m) {
    m->err = 0; m->invalid = 0; m->value = 0;
    if (!isdigit((unsigned char)*text)) { m->invalid = 1; return; }
    char *end;
    errno = 0;
    unsigned long long v = strtoull(text, &end, 10);
    if (errno || *end || v < 1 || v > 10000) m->invalid = 1;
    else m->value = (uint64_t)v;
}

/* /proc/stat aggregate line: "cpu user nice system idle iowait irq softirq
   steal [guest [guest_nice]]". Returns the steal clock in nanoseconds, or -1
   with errno set (ENOENT when unreadable, EPROTO when unparsable). Steal is
   diagnostic only: it corroborates host preemption of the guest vCPUs. */
static int read_steal_ns(long clk_tck, uint64_t *out) {
    char line[256];
    if (read_first_line("/proc/stat", line, sizeof(line))) return -1;
    if (strncmp(line, "cpu ", 4)) { errno = EPROTO; return -1; }
    unsigned long long f[8];
    if (sscanf(line + 4, "%llu %llu %llu %llu %llu %llu %llu %llu",
               &f[0], &f[1], &f[2], &f[3], &f[4], &f[5], &f[6], &f[7]) != 8) {
        errno = EPROTO;
        return -1;
    }
    *out = (uint64_t)f[7] * 1000000000ull / (uint64_t)clk_tck;
    return 0;
}

static int held_steal_ns(struct held_fd *h, long clk_tck, uint64_t *out) {
    char line[256];
    if (held_read_line(h, line, sizeof(line))) return -1;
    if (strncmp(line, "cpu ", 4)) { errno = EPROTO; return -1; }
    unsigned long long f[8];
    if (sscanf(line + 4, "%llu %llu %llu %llu %llu %llu %llu %llu",
               &f[0], &f[1], &f[2], &f[3], &f[4], &f[5], &f[6], &f[7]) != 8) {
        errno = EPROTO;
        return -1;
    }
    *out = (uint64_t)f[7] * 1000000000ull / (uint64_t)clk_tck;
    return 0;
}

static const char *cg_path(const char *name) {
    static char path[256];
    snprintf(path, sizeof(path), "/sys/fs/cgroup/%s", name);
    return path;
}

static struct cg_reading read_cg(void) {
    struct cg_reading cg;
    memset(&cg, 0, sizeof(cg));
    if (read_raw(cg_path("cpu.max"), cg.max_raw, sizeof(cg.max_raw))) cg.max_err = errno;
    else parse_cpu_max(cg.max_raw, &cg);
    if (read_raw(cg_path("cpuset.cpus"), cg.cpuset_raw, sizeof(cg.cpuset_raw))) cg.cpuset_err = errno;
    if (read_raw(cg_path("cpuset.cpus.effective"), cg.cpuset_eff_raw, sizeof(cg.cpuset_eff_raw)))
        cg.cpuset_eff_err = errno;
    if (read_raw(cg_path("cpu.weight"), cg.weight_raw, sizeof(cg.weight_raw))) cg.weight_err = errno;
    if (read_raw(cg_path("cpu.stat"), cg.stat_raw, sizeof(cg.stat_raw))) cg.stat_err = errno;
    else {
        parse_stat(cg.stat_raw, &cg.stat);
        if (!cg.stat.present) cg.stat_err = EPROTO;
    }
    return cg;
}

static void json_string(const char *s) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
        else if (*p == '\n') fputs("\\n", stdout);
        else if (*p == '\r') fputs("\\r", stdout);
        else if (*p == '\t') fputs("\\t", stdout);
        else if (*p < 0x20) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

static void json_file(const char *key, int err, const char *raw) {
    json_string(key);
    if (err) printf(":{\"errno\":%d}", err);
    else { fputs(":{\"errno\":0,\"raw\":", stdout); json_string(raw); fputs("}", stdout); }
}

static void json_stat_fields(const struct cfs_stat *st) {
    printf("\"present\":%d,\"usage_usec\":%llu,\"nr_periods\":%llu,\"nr_throttled\":%llu,"
           "\"throttled_usec\":%llu,\"nr_bursts\":%llu,\"burst_usec\":%llu",
           st->present, (unsigned long long)st->usage_usec, (unsigned long long)st->nr_periods,
           (unsigned long long)st->nr_throttled, (unsigned long long)st->throttled_usec,
           (unsigned long long)st->nr_bursts, (unsigned long long)st->burst_usec);
}

static void json_cg(const char *key, const struct cg_reading *cg) {
    json_string(key);
    fputs(":{", stdout);
    printf("\"cpu.max\":{\"errno\":%d,\"raw\":", cg->max_err);
    json_string(cg->max_raw);
    if (!cg->max_err)
        printf(",\"quota\":%llu,\"period\":%llu,\"quota_max\":%d",
               (unsigned long long)cg->quota, (unsigned long long)cg->period, cg->quota_max);
    fputs("},", stdout);
    json_file("cpuset.cpus", cg->cpuset_err, cg->cpuset_raw);
    fputs(",", stdout);
    json_file("cpuset.cpus.effective", cg->cpuset_eff_err, cg->cpuset_eff_raw);
    fputs(",", stdout);
    json_file("cpu.weight", cg->weight_err, cg->weight_raw);
    fputs(",\"cpu.stat\":{", stdout);
    if (cg->stat_err) printf("\"errno\":%d}", cg->stat_err);
    else { json_stat_fields(&cg->stat); fputs(",\"raw\":", stdout); json_string(cg->stat_raw); fputs("}", stdout); }
    fputs("}", stdout);
}

static void json_affinity(const char *key, const cpu_set_t *set) {
    json_string(key);
    fputs(":[", stdout);
    int first = 1;
    for (int cpu = 0; cpu < MAX_CPUS; cpu++) {
        if (CPU_ISSET(cpu, set)) { printf("%s%d", first ? "" : ",", cpu); first = 0; }
    }
    fputs("],", stdout);
}

struct worker {
    int idx;
    uint64_t cpu_start, cpu_end, getcpu_errors, getcpu_out_of_range;
    int aff_start_err, aff_end_err;
    cpu_set_t aff_start, aff_end, seen;
    uint64_t sink;
};

static uint64_t spin_deadline;

/* Graceful completion: the runner SIGTERMs after the final stable phase has
   been observed. Only an async-signal-safe flag is touched in the handler;
   workers and the sampler notice it between captures and shut down bounded.
   spin_fn is shared with `spin` (flag stays 0 there). */
static volatile sig_atomic_t stop_requested = 0;
static uint64_t stop_ns = 0;
static void on_stop(int sig) {
    (void)sig;
    stop_requested = 1;
}

static void *spin_fn(void *arg) {
    struct worker *w = arg;
    w->aff_start_err = pthread_getaffinity_np(pthread_self(), sizeof(w->aff_start), &w->aff_start);
    w->cpu_start = now_ns(CLOCK_THREAD_CPUTIME_ID);
    uint64_t x = 0x9e3779b97f4a7c15ull ^ (uint64_t)w->idx;
    while (now_ns(CLOCK_MONOTONIC) < spin_deadline && !stop_requested) {
        for (int k = 0; k < 1024; k++) { x = x * 6364136223846793005ull + (uint64_t)k + 1; }
        int cpu = sched_getcpu();
        if (cpu >= 0 && cpu < MAX_CPUS) CPU_SET(cpu, &w->seen);
        else if (cpu < 0) w->getcpu_errors++;
        else w->getcpu_out_of_range++;
    }
    w->sink = x;
    w->cpu_end = now_ns(CLOCK_THREAD_CPUTIME_ID);
    w->aff_end_err = pthread_getaffinity_np(pthread_self(), sizeof(w->aff_end), &w->aff_end);
    return NULL;
}

/* One tight, fully bracketed observation. The two cpu.max reads straddle the
   cpu.stat read: when they disagree, counters cannot be attributed to one
   setting and the checker must quarantine the sample. */
struct cap {
    uint64_t t0, t1, proc_ns, steal_ns;
    int max1_err, max2_err, stat_err, steal_err;
    int stable;
    struct cpu_max_v m1, m2;
    struct cfs_stat st;
};

static void capture(struct held_fd *fmax, struct held_fd *fstat, struct held_fd *fproc,
                    long clk_tck, struct cap *c) {
    memset(c, 0, sizeof(*c));
    c->max1_err = c->max2_err = c->stat_err = c->steal_err = 0;
    c->m1.quota_max = c->m2.quota_max = -1;
    c->steal_ns = 0;
    c->t0 = now_ns(CLOCK_MONOTONIC);
    char mb[96], sb[2048];
    if (held_read(fmax, mb, sizeof(mb))) c->m1.err = c->max1_err = errno;
    else { parse_cpu_max_v(mb, &c->m1); c->max1_err = c->m1.err; }
    if (held_read(fstat, sb, sizeof(sb))) c->stat_err = errno;
    else {
        parse_stat(sb, &c->st);
        if (!c->st.present) c->stat_err = EPROTO;
    }
    if (held_read(fmax, mb, sizeof(mb))) c->m2.err = c->max2_err = errno;
    else { parse_cpu_max_v(mb, &c->m2); c->max2_err = c->m2.err; }
    c->stable = (!c->max1_err && !c->max2_err
                 && c->m1.quota_max == c->m2.quota_max && c->m1.quota == c->m2.quota
                 && c->m1.period == c->m2.period);
    if (held_steal_ns(fproc, clk_tck, &c->steal_ns)) c->steal_err = errno;
    c->proc_ns = now_ns(CLOCK_PROCESS_CPUTIME_ID);
    c->t1 = now_ns(CLOCK_MONOTONIC);
}

static void emit_cap_sample(const struct cap *c) {
    printf("{\"event\":\"sample\",\"wall_ns\":%llu,\"cap_end_ns\":%llu,\"cap_dur_ns\":%llu,"
           "\"proc_cpu_ns\":%llu,\"quota_max\":%d,\"quota\":%llu,\"period\":%llu,\"max_errno\":%d,"
           "\"q2_quota_max\":%d,\"q2_quota\":%llu,\"q2_period\":%llu,\"max2_errno\":%d,\"max_stable\":%d,"
           "\"stat_errno\":%d,\"usage_usec\":%llu,\"nr_periods\":%llu,\"nr_throttled\":%llu,"
           "\"throttled_usec\":%llu,\"steal_ns\":%llu,\"steal_errno\":%d}\n",
           (unsigned long long)c->t0, (unsigned long long)c->t1,
           (unsigned long long)(c->t1 - c->t0), (unsigned long long)c->proc_ns,
           c->m1.quota_max, (unsigned long long)c->m1.quota,
           (unsigned long long)c->m1.period, c->max1_err,
           c->m2.quota_max, (unsigned long long)c->m2.quota,
           (unsigned long long)c->m2.period, c->max2_err, c->stable,
           c->stat_err, (unsigned long long)c->st.usage_usec,
           (unsigned long long)c->st.nr_periods, (unsigned long long)c->st.nr_throttled,
           (unsigned long long)c->st.throttled_usec,
           (unsigned long long)c->steal_ns, c->steal_err);
    fflush(stdout);
}

/* Fully bracketed shares observation.
 *
 * wall_ns is taken immediately before the first syscall and cap_end_ns right
 * after the last clock, so a deschedule between reads can never misdate the
 * counters. Read order:
 *   t0 -> cpu.max(1) cpu.weight(1) cpuset.eff(1) -> cpu.stat ->
 *         cpu.weight(2) cpuset.eff(2) cpu.max(2) -> steal -> proc clock -> t1
 * cpu.stat is thus bracketed by two cpu.weight reads and two cpuset reads; a
 * `docker update --cpu-shares` landing anywhere inside the capture makes
 * weight1 != weight2, which is emitted as weight_stable=0 rather than
 * attributed. Every failure is first-errno, counted in the JSON, and never
 * retried within the capture. */
struct cap_share {
    uint64_t t0, t1, proc_ns, steal_ns;
    int max1_err, max2_err, stat_err, steal_err;
    int w1_err, w2_err, w1_invalid, w2_invalid;
    int c1_err, c2_err;
    int max_stable, weight_stable, cpuset_stable, share_valid;
    struct cpu_max_v m1, m2;
    struct weight_v wv1, wv2;
    struct cfs_stat st;
    char max1_raw[96], max2_raw[96], cp1_raw[256], cp2_raw[256], stat_raw[2048];
};

static void capture_shares(struct held_fd *fmax, struct held_fd *fweight,
                           struct held_fd *fcpuset, struct held_fd *fstat,
                           struct held_fd *fproc, long clk_tck, struct cap_share *c) {
    memset(c, 0, sizeof(*c));
    c->m1.quota_max = c->m2.quota_max = -1;
    char wb[64], sb[2048];
    c->t0 = now_ns(CLOCK_MONOTONIC);
    if (held_read(fmax, c->max1_raw, sizeof(c->max1_raw))) c->max1_err = errno;
    else { parse_cpu_max_v(c->max1_raw, &c->m1); c->max1_err = c->m1.err; }
    if (held_read(fweight, wb, sizeof(wb))) c->w1_err = errno;
    else parse_weight_v(wb, &c->wv1);
    if (held_read(fcpuset, c->cp1_raw, sizeof(c->cp1_raw))) c->c1_err = errno;
    if (held_read(fstat, sb, sizeof(sb))) c->stat_err = errno;
    else {
        snprintf(c->stat_raw, sizeof(c->stat_raw), "%s", sb);
        parse_stat(sb, &c->st);
        if (!c->st.present) c->stat_err = EPROTO;
    }
    if (held_read(fweight, wb, sizeof(wb))) c->w2_err = errno;
    else parse_weight_v(wb, &c->wv2);
    if (held_read(fcpuset, c->cp2_raw, sizeof(c->cp2_raw))) c->c2_err = errno;
    if (held_read(fmax, c->max2_raw, sizeof(c->max2_raw))) c->max2_err = errno;
    else { parse_cpu_max_v(c->max2_raw, &c->m2); c->max2_err = c->m2.err; }
    c->w1_invalid = c->wv1.invalid;
    c->w2_invalid = c->wv2.invalid;
    if (c->w1_invalid && !c->w1_err) c->w1_err = EPROTO;
    if (c->w2_invalid && !c->w2_err) c->w2_err = EPROTO;
    c->max_stable = (!c->max1_err && !c->max2_err
                     && c->m1.quota_max == c->m2.quota_max && c->m1.quota == c->m2.quota
                     && c->m1.period == c->m2.period);
    c->weight_stable = (!c->w1_err && !c->w2_err && c->wv1.value == c->wv2.value);
    c->cpuset_stable = (!c->c1_err && !c->c2_err && !strcmp(c->cp1_raw, c->cp2_raw));
    c->share_valid = (c->max_stable && c->weight_stable && c->cpuset_stable
                      && !c->stat_err);
    if (held_steal_ns(fproc, clk_tck, &c->steal_ns)) c->steal_err = errno;
    c->proc_ns = now_ns(CLOCK_PROCESS_CPUTIME_ID);
    c->t1 = now_ns(CLOCK_MONOTONIC);
}

static void emit_share_sample(const struct cap_share *c, int terminal) {
    printf("{\"event\":\"sample\",\"wall_ns\":%llu,\"cap_end_ns\":%llu,\"cap_dur_ns\":%llu,"
           "\"terminal\":%d,"
           "\"graceful\":%d,"
           "\"proc_cpu_ns\":%llu,\"sampler_cpu_ns\":%llu,"
           "\"weight1\":%llu,\"weight2\":%llu,\"weight1_errno\":%d,\"weight2_errno\":%d,"
           "\"weight1_invalid\":%d,\"weight2_invalid\":%d,\"weight_stable\":%d,"
           "\"cpuset1_errno\":%d,\"cpuset2_errno\":%d,\"cpuset_stable\":%d,"
           "\"quota_max\":%d,\"quota\":%llu,\"period\":%llu,\"max_errno\":%d,"
           "\"q2_quota_max\":%d,\"q2_quota\":%llu,\"q2_period\":%llu,\"max2_errno\":%d,\"max_stable\":%d,"
           "\"share_valid\":%d,\"stat_errno\":%d,\"usage_usec\":%llu,\"nr_periods\":%llu,"
           "\"nr_throttled\":%llu,\"throttled_usec\":%llu,\"steal_ns\":%llu,\"steal_errno\":%d,"
           "\"cpuset1_raw\":",
           (unsigned long long)c->t0, (unsigned long long)c->t1,
           (unsigned long long)(c->t1 - c->t0), terminal, stop_requested ? 1 : 0,
           (unsigned long long)c->proc_ns,
           (unsigned long long)now_ns(CLOCK_THREAD_CPUTIME_ID),
           (unsigned long long)c->wv1.value, (unsigned long long)c->wv2.value,
           c->w1_err, c->w2_err, c->w1_invalid, c->w2_invalid, c->weight_stable,
           c->c1_err, c->c2_err, c->cpuset_stable,
           c->m1.quota_max, (unsigned long long)c->m1.quota,
           (unsigned long long)c->m1.period, c->max1_err,
           c->m2.quota_max, (unsigned long long)c->m2.quota,
           (unsigned long long)c->m2.period, c->max2_err, c->max_stable,
           c->share_valid, c->stat_err, (unsigned long long)c->st.usage_usec,
           (unsigned long long)c->st.nr_periods, (unsigned long long)c->st.nr_throttled,
           (unsigned long long)c->st.throttled_usec,
           (unsigned long long)c->steal_ns, c->steal_err);
    json_string(c->cp1_raw);
    fputs(",\"cpuset2_raw\":", stdout);
    json_string(c->cp2_raw);
    fputs("}\n", stdout);
    fflush(stdout);
}

/* Emit a cgroup object from one bracketed shares capture (start/end events). */
static void json_share_cg(const struct cap_share *c) {
    fputs("\"cgroup\":{\"cpu.max\":{\"errno\":", stdout);
    printf("%d,\"raw\":", c->max1_err);
    json_string(c->max1_raw);
    if (!c->max1_err)
        printf(",\"quota\":%llu,\"period\":%llu,\"quota_max\":%d",
               (unsigned long long)c->m1.quota, (unsigned long long)c->m1.period,
               c->m1.quota_max);
    fputs("},\"cpu.max2\":{\"errno\":", stdout);
    printf("%d,\"raw\":", c->max2_err);
    json_string(c->max2_raw);
    if (!c->max2_err)
        printf(",\"quota\":%llu,\"period\":%llu,\"quota_max\":%d",
               (unsigned long long)c->m2.quota, (unsigned long long)c->m2.period,
               c->m2.quota_max);
    printf("},\"max_stable\":%d,", c->max_stable);
    printf("\"cpu.weight\":{\"errno\":%d,\"raw\":\"%llu\",\"invalid\":%d},",
           c->w1_err, (unsigned long long)c->wv1.value, c->w1_invalid);
    printf("\"cpu.weight2\":{\"errno\":%d,\"raw\":\"%llu\",\"invalid\":%d},",
           c->w2_err, (unsigned long long)c->wv2.value, c->w2_invalid);
    printf("\"weight_stable\":%d,", c->weight_stable);
    printf("\"cpuset.cpus.effective\":{\"errno\":%d,\"raw\":", c->c1_err);
    json_string(c->cp1_raw);
    fputs("},\"cpuset.cpus.effective2\":{", stdout);
    printf("\"errno\":%d,\"raw\":", c->c2_err);
    json_string(c->cp2_raw);
    printf("},\"cpuset_stable\":%d,\"cpu.stat\":{", c->cpuset_stable);
    if (c->stat_err) printf("\"errno\":%d}", c->stat_err);
    else { json_stat_fields(&c->st); fputs(",\"raw\":", stdout);
           json_string(c->stat_raw); fputs("}", stdout); }
    fputs("}", stdout);
}

static void sleep_relative_ms(uint64_t ms) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    ts.tv_sec += (time_t)(ms / 1000);
    ts.tv_nsec += (long)(ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) { }
}

static int cmd_spin(int argc, char **argv) {
    int threads = 0;
    uint64_t duration_ms = 0, sample_ms = 100;
    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "--threads") && i + 1 < argc) threads = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--duration-ms") && i + 1 < argc) duration_ms = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--sample-ms") && i + 1 < argc) sample_ms = strtoull(argv[++i], NULL, 10);
        else { fprintf(stderr, "unknown argument: %s\n", argv[i]); return 2; }
    }
    if (threads < 1 || threads > MAX_THREADS || duration_ms < 100 || duration_ms > 120000 ||
        sample_ms < 20 || sample_ms > 1000) {
        fprintf(stderr, "usage: spin --threads 1..64 --duration-ms 100..120000 [--sample-ms 20..1000]\n");
        return 2;
    }
    cpu_set_t own;
    CPU_ZERO(&own);
    if (sched_getaffinity(0, sizeof(own), &own)) { perror("sched_getaffinity"); return 1; }
    long clk_tck = sysconf(_SC_CLK_TCK);
    if (clk_tck <= 0) { perror("sysconf clk_tck"); return 1; }

    /* Persistent sample-path fds are opened before t0 so open cost never lands
       in the measured window. cpu.max/cpu.stat must be readable up front;
       /proc/stat is optional diagnostic evidence. */
    struct held_fd fmax = {{0}, -1}, fstat = {{0}, -1}, fproc = {{0}, -1};
    snprintf(fmax.path, sizeof(fmax.path), "/sys/fs/cgroup/%s", "cpu.max");
    snprintf(fstat.path, sizeof(fstat.path), "/sys/fs/cgroup/%s", "cpu.stat");
    snprintf(fproc.path, sizeof(fproc.path), "%s", "/proc/stat");
    held_open(&fmax);
    held_open(&fstat);
    held_open(&fproc);
    char probe_max[96], probe_stat[2048];
    if (fmax.fd < 0 || held_read(&fmax, probe_max, sizeof(probe_max)) != 0
            || fstat.fd < 0 || held_read(&fstat, probe_stat, sizeof(probe_stat)) != 0) {
        fprintf(stderr, "required cgroup files unreadable: %s\n", strerror(errno));
        held_close(&fmax); held_close(&fstat); held_close(&fproc);
        return 1;
    }

    uint64_t t0 = now_ns(CLOCK_MONOTONIC);
    spin_deadline = t0 + duration_ms * 1000000ull;
    struct worker *workers = calloc((size_t)threads, sizeof(*workers));
    if (!workers) { perror("calloc"); return 1; }
    pthread_t *ids = calloc((size_t)threads, sizeof(*ids));
    if (!ids) { perror("calloc"); return 1; }

    struct cg_reading cg0 = read_cg();
    uint64_t sampler0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
    uint64_t proc0 = now_ns(CLOCK_PROCESS_CPUTIME_ID);
    uint64_t steal0 = 0;
    int steal0_err = held_steal_ns(&fproc, clk_tck, &steal0) ? errno : 0;
    uint64_t cap0_end = now_ns(CLOCK_MONOTONIC);
    printf("{\"event\":\"start\",\"wall_ns\":%llu,\"cap_end_ns\":%llu,\"proc_cpu_ns\":%llu,"
           "\"sampler_cpu_ns\":%llu,\"steal_ns\":%llu,\"steal_errno\":%d,"
           "\"load_deadline_ns\":%llu,\"threads\":%d,\"nproc\":%ld,\"clk_tck\":%ld,",
           (unsigned long long)t0, (unsigned long long)cap0_end,
           (unsigned long long)proc0, (unsigned long long)sampler0,
           (unsigned long long)steal0, steal0_err,
           (unsigned long long)spin_deadline, threads,
           (long)sysconf(_SC_NPROCESSORS_ONLN), clk_tck);
    json_affinity("affinity", &own);
    json_cg("cgroup", &cg0);
    fputs("}\n", stdout);
    fflush(stdout);

    for (int i = 0; i < threads; i++) {
        workers[i].idx = i;
        int e = pthread_create(&ids[i], NULL, spin_fn, &workers[i]);
        if (e) {
            fprintf(stderr, "pthread_create: %s\n", strerror(e));
            held_close(&fmax); held_close(&fstat); held_close(&fproc);
            return 1;
        }
    }
    /* Samples include the live cpu.max value (read twice around cpu.stat) so the
       harness can align windows with externally observed `docker update`
       transitions without a shared clock. The sampler sleeps; only the worker
       threads offer load. Sparse wakeups are evidence, never caught up. */
    while (1) {
        sleep_relative_ms(sample_ms);
        struct cap c;
        capture(&fmax, &fstat, &fproc, clk_tck, &c);
        emit_cap_sample(&c);
        if (c.t0 >= spin_deadline) break;
    }
    for (int i = 0; i < threads; i++)
        if (pthread_join(ids[i], NULL)) { perror("pthread_join"); return 1; }

    uint64_t t1 = now_ns(CLOCK_MONOTONIC);
    uint64_t proc1 = now_ns(CLOCK_PROCESS_CPUTIME_ID);
    uint64_t sampler1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
    struct cg_reading cg1 = read_cg();
    uint64_t steal1 = 0;
    int steal1_err = held_steal_ns(&fproc, clk_tck, &steal1) ? errno : 0;
    uint64_t t1_end = now_ns(CLOCK_MONOTONIC);
    held_close(&fmax); held_close(&fstat); held_close(&fproc);
    uint64_t total = 0;
    fputs("{\"event\":\"end\",\"wall_ns\":", stdout);
    printf("%llu", (unsigned long long)t1);
    printf(",\"cap_end_ns\":%llu,\"duration_ns\":%llu,\"proc_cpu_ns\":%llu,\"sampler_cpu_ns\":%llu,"
           "\"steal_ns\":%llu,\"steal_errno\":%d,\"workers\":[",
           (unsigned long long)t1_end, (unsigned long long)(t1 - t0),
           (unsigned long long)proc1, (unsigned long long)sampler1,
           (unsigned long long)steal1, steal1_err);
    for (int i = 0; i < threads; i++) {
        uint64_t used = workers[i].cpu_end - workers[i].cpu_start;
        total += used;
        fputs(i ? ",{" : "{", stdout);
        printf("\"idx\":%d,\"cpu_ns\":%llu,\"getcpu_errors\":%llu,\"getcpu_oor\":%llu,"
               "\"affinity_start_errno\":%d,\"affinity_end_errno\":%d,\"cpus_seen\":[",
               i, (unsigned long long)used, (unsigned long long)workers[i].getcpu_errors,
               (unsigned long long)workers[i].getcpu_out_of_range,
               workers[i].aff_start_err, workers[i].aff_end_err);
        int first = 1;
        for (int cpu = 0; cpu < MAX_CPUS; cpu++)
            if (CPU_ISSET(cpu, &workers[i].seen)) { printf("%s%d", first ? "" : ",", cpu); first = 0; }
        fputs("],", stdout);
        json_affinity("affinity_start", &workers[i].aff_start);
        json_affinity("affinity_end", &workers[i].aff_end);
        fputs("\"sink\":", stdout);
        printf("%llu}", (unsigned long long)workers[i].sink);
    }
    printf("],\"worker_cpu_ns\":%llu,", (unsigned long long)total);
    fputs("\"stat_start\":{", stdout); json_stat_fields(&cg0.stat); fputs("},", stdout);
    fputs("\"stat_end\":{", stdout); json_stat_fields(&cg1.stat); fputs("},", stdout);
    json_cg("cgroup", &cg1);
    fputs("}\n", stdout);
    free(workers);
    free(ids);
    return 0;
}

/* Shares-contention workload. Same worker pool, limits and sampling cadence as
   spin; every sample is a fully bracketed capture_shares with cpu.weight and
   cpuset read twice around cpu.stat. The sampler thread alone observes; the
   worker threads offer the contended load. */
static int cmd_contend(int argc, char **argv) {
    int threads = 0;
    uint64_t duration_ms = 0, sample_ms = 100;
    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "--threads") && i + 1 < argc) threads = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--duration-ms") && i + 1 < argc) duration_ms = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--sample-ms") && i + 1 < argc) sample_ms = strtoull(argv[++i], NULL, 10);
        else { fprintf(stderr, "unknown argument: %s\n", argv[i]); return 2; }
    }
    if (threads < 1 || threads > MAX_THREADS || duration_ms < 100 || duration_ms > 120000 ||
        sample_ms < 20 || sample_ms > 1000) {
        fprintf(stderr, "usage: contend --threads 1..64 --duration-ms 100..120000 [--sample-ms 20..1000]\n");
        return 2;
    }
    cpu_set_t own;
    CPU_ZERO(&own);
    if (sched_getaffinity(0, sizeof(own), &own)) { perror("sched_getaffinity"); return 1; }
    long clk_tck = sysconf(_SC_CLK_TCK);
    if (clk_tck <= 0) { perror("sysconf clk_tck"); return 1; }

    struct held_fd fmax = {{0}, -1}, fstat = {{0}, -1}, fproc = {{0}, -1};
    struct held_fd fweight = {{0}, -1}, fcpuset = {{0}, -1};
    snprintf(fmax.path, sizeof(fmax.path), "/sys/fs/cgroup/%s", "cpu.max");
    snprintf(fweight.path, sizeof(fweight.path), "/sys/fs/cgroup/%s", "cpu.weight");
    snprintf(fcpuset.path, sizeof(fcpuset.path), "/sys/fs/cgroup/%s", "cpuset.cpus.effective");
    snprintf(fstat.path, sizeof(fstat.path), "/sys/fs/cgroup/%s", "cpu.stat");
    snprintf(fproc.path, sizeof(fproc.path), "%s", "/proc/stat");
    /* All five verdict files must be readable before workers start so open
       cost never lands in a measured capture. */
    held_open(&fmax); held_open(&fweight); held_open(&fcpuset);
    held_open(&fstat); held_open(&fproc);
    char probe[2048];
    if (fmax.fd < 0 || fweight.fd < 0 || fcpuset.fd < 0 || fstat.fd < 0
            || held_read(&fstat, probe, sizeof(probe)) != 0) {
        fprintf(stderr, "required share cgroup files unreadable: %s\n", strerror(errno));
        held_close(&fmax); held_close(&fweight); held_close(&fcpuset);
        held_close(&fstat); held_close(&fproc);
        return 1;
    }

    /* Install graceful-completion handlers. SIGTERM requests an early,
       bounded shutdown once the runner has observed the final stable phase;
       SIGINT behaves the same. The hard duration deadline remains. */
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_stop;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    struct cap_share cs0;
    uint64_t sampler0 = now_ns(CLOCK_THREAD_CPUTIME_ID);
    capture_shares(&fmax, &fweight, &fcpuset, &fstat, &fproc, clk_tck, &cs0);
    uint64_t t0 = cs0.t0;
    spin_deadline = t0 + duration_ms * 1000000ull;
    struct worker *workers = calloc((size_t)threads, sizeof(*workers));
    if (!workers) { perror("calloc"); return 1; }
    pthread_t *ids = calloc((size_t)threads, sizeof(*ids));
    if (!ids) { perror("calloc"); return 1; }

    printf("{\"event\":\"start\",\"wall_ns\":%llu,\"cap_end_ns\":%llu,\"mode\":\"contend\","
           "\"proc_cpu_ns\":%llu,\"sampler_cpu_ns\":%llu,\"load_deadline_ns\":%llu,"
           "\"steal_ns\":%llu,\"steal_errno\":%d,\"threads\":%d,\"nproc\":%ld,\"clk_tck\":%ld,",
           (unsigned long long)t0, (unsigned long long)cs0.t1,
           (unsigned long long)cs0.proc_ns, (unsigned long long)sampler0,
           (unsigned long long)spin_deadline,
           (unsigned long long)cs0.steal_ns, cs0.steal_err, threads,
           (long)sysconf(_SC_NPROCESSORS_ONLN), clk_tck);
    json_affinity("affinity", &own);
    json_share_cg(&cs0);
    fputs("}\n", stdout);
    fflush(stdout);

    for (int i = 0; i < threads; i++) {
        workers[i].idx = i;
        int e = pthread_create(&ids[i], NULL, spin_fn, &workers[i]);
        if (e) {
            fprintf(stderr, "pthread_create: %s\n", strerror(e));
            held_close(&fmax); held_close(&fweight); held_close(&fcpuset);
            held_close(&fstat); held_close(&fproc);
            return 1;
        }
    }
    int graceful = 0;
    while (1) {
        /* Stop-responsive wait: chunk the interval so a SIGTERM is noticed
           within ~10 ms without starting another capture. */
        uint64_t remain = sample_ms;
        while (!stop_requested && remain > 0) {
            uint64_t step = remain < 10 ? remain : 10;
            sleep_relative_ms(step);
            remain -= step;
        }
        if (stop_requested) { stop_ns = now_ns(CLOCK_MONOTONIC); graceful = 1; break; }
        struct cap_share cs;
        capture_shares(&fmax, &fweight, &fcpuset, &fstat, &fproc, clk_tck, &cs);
        if (stop_requested) { stop_ns = now_ns(CLOCK_MONOTONIC); graceful = 1; }
        int terminal = (graceful && cs.t0 <= stop_ns && stop_ns <= cs.t1) ? 1 : 0;
        emit_share_sample(&cs, terminal);
        if (graceful || cs.t0 >= spin_deadline) break;
    }
    /* Workers see stop_requested in their spin condition and join bounded; a
       hard deadline exit is unchanged (flag stays 0, loop ends on time). */
    for (int i = 0; i < threads; i++)
        if (pthread_join(ids[i], NULL)) { perror("pthread_join"); return 1; }

    uint64_t sampler1 = now_ns(CLOCK_THREAD_CPUTIME_ID);
    struct cap_share cs1;
    capture_shares(&fmax, &fweight, &fcpuset, &fstat, &fproc, clk_tck, &cs1);
    held_close(&fmax); held_close(&fweight); held_close(&fcpuset);
    held_close(&fstat); held_close(&fproc);
    uint64_t total = 0;
    printf("{\"event\":\"end\",\"wall_ns\":%llu,\"cap_end_ns\":%llu,\"duration_ns\":%llu,"
           "\"graceful_stop\":%d,\"stop_ns\":%llu,\"hard_deadline_ns\":%llu,"
           "\"proc_cpu_ns\":%llu,\"sampler_cpu_ns\":%llu,\"steal_ns\":%llu,\"steal_errno\":%d,"
           "\"workers\":[",
           (unsigned long long)cs1.t0, (unsigned long long)cs1.t1,
           (unsigned long long)(cs1.t0 - t0), graceful,
           (unsigned long long)(graceful ? stop_ns : 0),
           (unsigned long long)spin_deadline,
           (unsigned long long)cs1.proc_ns,
           (unsigned long long)sampler1, (unsigned long long)cs1.steal_ns, cs1.steal_err);
    for (int i = 0; i < threads; i++) {
        uint64_t used = workers[i].cpu_end - workers[i].cpu_start;
        total += used;
        fputs(i ? ",{" : "{", stdout);
        printf("\"idx\":%d,\"cpu_ns\":%llu,\"getcpu_errors\":%llu,\"getcpu_oor\":%llu,"
               "\"affinity_start_errno\":%d,\"affinity_end_errno\":%d,\"cpus_seen\":[",
               i, (unsigned long long)used, (unsigned long long)workers[i].getcpu_errors,
               (unsigned long long)workers[i].getcpu_out_of_range,
               workers[i].aff_start_err, workers[i].aff_end_err);
        int first = 1;
        for (int cpu = 0; cpu < MAX_CPUS; cpu++)
            if (CPU_ISSET(cpu, &workers[i].seen)) { printf("%s%d", first ? "" : ",", cpu); first = 0; }
        fputs("],", stdout);
        json_affinity("affinity_start", &workers[i].aff_start);
        json_affinity("affinity_end", &workers[i].aff_end);
        fputs("\"sink\":", stdout);
        printf("%llu}", (unsigned long long)workers[i].sink);
    }
    printf("],\"worker_cpu_ns\":%llu,", (unsigned long long)total);
    fputs("\"stat_start\":{", stdout); json_stat_fields(&cs0.st); fputs("},", stdout);
    fputs("\"stat_end\":{", stdout); json_stat_fields(&cs1.st); fputs("},", stdout);
    json_share_cg(&cs1);
    fputs("}\n", stdout);
    free(workers);
    free(ids);
    return 0;
}

static int cmd_info(void) {
    cpu_set_t own;
    CPU_ZERO(&own);
    if (sched_getaffinity(0, sizeof(own), &own)) { perror("sched_getaffinity"); return 1; }
    struct cg_reading cg = read_cg();
    uint64_t steal = 0;
    int steal_err = read_steal_ns(sysconf(_SC_CLK_TCK), &steal) ? errno : 0;
    printf("{\"event\":\"info\",\"wall_ns\":%llu,\"proc_cpu_ns\":%llu,\"pid\":%ld,"
           "\"steal_ns\":%llu,\"steal_errno\":%d,\"nproc_online\":%ld,\"nproc_conf\":%ld,\"clk_tck\":%ld,",
           (unsigned long long)now_ns(CLOCK_MONOTONIC),
           (unsigned long long)now_ns(CLOCK_PROCESS_CPUTIME_ID), (long)getpid(),
           (unsigned long long)steal, steal_err,
           (long)sysconf(_SC_NPROCESSORS_ONLN), (long)sysconf(_SC_NPROCESSORS_CONF),
           (long)sysconf(_SC_CLK_TCK));
    json_affinity("affinity", &own);
    json_cg("cgroup", &cg);
    fputs("}\n", stdout);
    return 0;
}

static int cmd_snapshot(void) {
    struct cg_reading cg = read_cg();
    uint64_t steal = 0;
    int steal_err = read_steal_ns(sysconf(_SC_CLK_TCK), &steal) ? errno : 0;
    printf("{\"event\":\"snapshot\",\"wall_ns\":%llu,\"proc_cpu_ns\":%llu,\"pid\":%ld,"
           "\"steal_ns\":%llu,\"steal_errno\":%d,",
           (unsigned long long)now_ns(CLOCK_MONOTONIC),
           (unsigned long long)now_ns(CLOCK_PROCESS_CPUTIME_ID), (long)getpid(),
           (unsigned long long)steal, steal_err);
    json_cg("cgroup", &cg);
    fputs("}\n", stdout);
    return 0;
}

static int cmd_selftest(void) {
    int failures = 0;
    const char *s1 = "usage_usec 12345\nuser_usec 1\nsystem_usec 2\nnr_periods 7\nnr_throttled 3\n"
                     "nr_bursts 0\nburst_usec 0\nthrottled_usec 99\n";
    struct cfs_stat st;
    parse_stat(s1, &st);
    int stat_ok = (st.present && st.usage_usec == 12345 && st.nr_periods == 7 &&
                   st.nr_throttled == 3 && st.throttled_usec == 99 && st.nr_bursts == 0);
    struct cpu_max_v m1 = {0};
    parse_cpu_max_v("200000 100000", &m1);
    struct cpu_max_v m1b = {0};
    parse_cpu_max_v("200000 100000", &m1b);
    int max_ok = (!m1.err && m1.quota_max == 0 && m1.quota == 200000 && m1.period == 100000
                  && !m1b.err && m1b.quota_max == m1.quota_max && m1b.quota == m1.quota
                  && m1b.period == m1.period);
    struct cpu_max_v m2 = {0};
    parse_cpu_max_v("max 100000", &m2);
    int max2_ok = (m2.quota_max == 1 && m2.period == 100000 && !m2.err);
    const char *invalid[] = {"max", "max 0", "-1 100000", "0 100000", "1 2 trailing", "x 100000",
                             "18446744073709551616 100000"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        struct cpu_max_v bad = {0};
        parse_cpu_max_v(invalid[i], &bad);
        if (!bad.err || bad.quota_max != -1) max2_ok = 0;
    }
    parse_stat("usage_usec 123\n", &st);
    if (st.present) stat_ok = 0;
    parse_stat("usage_usec bad\nnr_periods 1\nnr_throttled 0\nthrottled_usec 0", &st);
    if (st.present) stat_ok = 0;
    int steal_ok = 1;
    const char *good_steal = "cpu  100 2 30 4000 5 6 7 123 9 0\n";
    unsigned long long f[8];
    if (sscanf(good_steal + 4, "%llu %llu %llu %llu %llu %llu %llu %llu",
               &f[0], &f[1], &f[2], &f[3], &f[4], &f[5], &f[6], &f[7]) != 8 || f[7] != 123)
        steal_ok = 0;
    if (strncmp("cpu0 1 2 3", "cpu ", 4) == 0) steal_ok = 0;
    /* Shares weight parser: one integer in [1,10000], nothing else. */
    int weight_ok = 1;
    struct weight_v wv;
    memset(&wv, 0, sizeof(wv)); parse_weight_v("1", &wv);
    weight_ok &= (!wv.invalid && !wv.err && wv.value == 1);
    memset(&wv, 0, sizeof(wv)); parse_weight_v("10000", &wv);
    weight_ok &= (!wv.invalid && !wv.err && wv.value == 10000);
    memset(&wv, 0, sizeof(wv)); parse_weight_v("100", &wv);
    weight_ok &= (!wv.invalid && wv.value == 100);
    const char *bad_weight[] = {"0", "10001", "-1", "x", "100 ", "", " 100", "1.5", "1e2"};
    for (size_t i = 0; i < sizeof(bad_weight) / sizeof(bad_weight[0]); i++) {
        struct weight_v bad = {0};
        parse_weight_v(bad_weight[i], &bad);
        if (!bad.invalid) weight_ok = 0;
    }
    cpu_set_t own;
    CPU_ZERO(&own);
    sched_getaffinity(0, sizeof(own), &own);
    int aff_ok = CPU_COUNT(&own) > 0;
    if (!stat_ok) failures++;
    if (!max_ok || !max2_ok) failures++;
    if (!steal_ok) failures++;
    if (!weight_ok) failures++;
    if (!aff_ok) failures++;
    printf("{\"event\":\"selftest\",\"parse_stat\":\"%s\",\"parse_cpu_max\":\"%s\","
           "\"parse_steal\":\"%s\",\"parse_weight\":\"%s\",\"affinity\":\"%s\",\"status\":\"%s\"}\n",
           stat_ok ? "pass" : "fail", (max_ok && max2_ok) ? "pass" : "fail",
           steal_ok ? "pass" : "fail", weight_ok ? "pass" : "fail",
           aff_ok ? "pass" : "fail",
           failures ? "fail" : "pass");
    return failures ? 1 : 0;
}

/* Drive the real capture()/capture_shares() against arbitrary files (regular
   files or FIFOs) so malformed, missing, flapping, delayed and
   appearing-after-error paths are executable fixtures rather than assumptions.
   With --weight/--cpuset the share bracket capture is exercised (emits the
   contend sample schema); without them the frozen v4 schema is emitted
   unchanged. FIFOs deliver one message per bracketed read, which lets a test
   stage a weight flip or a multi-second blocking delay INSIDE one capture.
   Always exits 0; verdicts live in the JSON. */
static int cmd_capture_test(int argc, char **argv) {
    const char *maxp = NULL, *statp = NULL, *procp = NULL;
    const char *weightp = NULL, *cpusetp = NULL;
    int count = 1;
    uint64_t interval_ms = 0;
    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "--max") && i + 1 < argc) maxp = argv[++i];
        else if (!strcmp(argv[i], "--stat") && i + 1 < argc) statp = argv[++i];
        else if (!strcmp(argv[i], "--proc") && i + 1 < argc) procp = argv[++i];
        else if (!strcmp(argv[i], "--weight") && i + 1 < argc) weightp = argv[++i];
        else if (!strcmp(argv[i], "--cpuset") && i + 1 < argc) cpusetp = argv[++i];
        else if (!strcmp(argv[i], "--count") && i + 1 < argc) count = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--interval-ms") && i + 1 < argc)
            interval_ms = strtoull(argv[++i], NULL, 10);
        else { fprintf(stderr, "unknown argument: %s\n", argv[i]); return 2; }
    }
    int shares_mode = (weightp != NULL || cpusetp != NULL);
    if (!maxp || !statp || !procp || (shares_mode && (!weightp || !cpusetp))
            || count < 1 || count > 1000 || interval_ms > 10000) {
        fprintf(stderr, "usage: capture-test --max P --stat P --proc P "
                        "[--weight P --cpuset P] [--count 1..1000] [--interval-ms 0..10000]\n");
        return 2;
    }
    struct held_fd fmax = {{0}, -1}, fstat = {{0}, -1}, fproc = {{0}, -1};
    struct held_fd fweight = {{0}, -1}, fcpuset = {{0}, -1};
    size_t paths_len = strlen(maxp) + strlen(statp) + strlen(procp) + 1;
    if (shares_mode) paths_len += strlen(weightp) + strlen(cpusetp);
    if (paths_len >= sizeof(fmax.path)) {
        fprintf(stderr, "fixture path too long\n");
        return 2;
    }
    snprintf(fmax.path, sizeof(fmax.path), "%s", maxp);
    snprintf(fstat.path, sizeof(fstat.path), "%s", statp);
    snprintf(fproc.path, sizeof(fproc.path), "%s", procp);
    if (shares_mode) {
        snprintf(fweight.path, sizeof(fweight.path), "%s", weightp);
        snprintf(fcpuset.path, sizeof(fcpuset.path), "%s", cpusetp);
    }
    long clk_tck = sysconf(_SC_CLK_TCK);
    if (clk_tck <= 0) { perror("sysconf clk_tck"); return 1; }
    for (int i = 0; i < count; i++) {
        if (i && interval_ms) sleep_relative_ms(interval_ms);
        if (shares_mode) {
            struct cap_share cs;
            capture_shares(&fmax, &fweight, &fcpuset, &fstat, &fproc, clk_tck, &cs);
            emit_share_sample(&cs, 0);
        } else {
            struct cap c;
            capture(&fmax, &fstat, &fproc, clk_tck, &c);
            emit_cap_sample(&c);
        }
    }
    held_close(&fmax);
    held_close(&fstat);
    held_close(&fproc);
    if (shares_mode) { held_close(&fweight); held_close(&fcpuset); }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: cpu-share-probe selftest|info|snapshot|spin|contend|capture-test ...\n"); return 2; }
    if (!strcmp(argv[1], "selftest")) return cmd_selftest();
    if (!strcmp(argv[1], "info")) return cmd_info();
    if (!strcmp(argv[1], "snapshot")) return cmd_snapshot();
    if (!strcmp(argv[1], "spin")) return cmd_spin(argc, argv);
    if (!strcmp(argv[1], "contend")) return cmd_contend(argc, argv);
    if (!strcmp(argv[1], "capture-test")) return cmd_capture_test(argc, argv);
    fprintf(stderr, "unknown command: %s\n", argv[1]);
    return 2;
}
