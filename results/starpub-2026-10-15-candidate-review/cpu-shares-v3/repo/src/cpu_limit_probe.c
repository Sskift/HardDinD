/* cpu_limit_probe - observe CFS bandwidth and cpuset enforcement from inside
 * a container. Read-only: it never writes cgroup files or changes its affinity.
 *
 *   cpu-limit-probe selftest
 *   cpu-limit-probe info
 *   cpu-limit-probe snapshot
 *   cpu-limit-probe spin --threads N --duration-ms T [--sample-ms S]
 *
 * Every normal output line is one JSON object (JSONL). All timestamps and
 * counters are integers; rate math belongs to the owner-run Python harness so
 * the probe output stays raw evidence. Static, musl/glibc independent.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
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

struct cg_reading {
    int max_err, cpuset_err, cpuset_eff_err, weight_err, stat_err;
    int quota_max;
    uint64_t quota, period;
    char max_raw[96], cpuset_raw[8192], cpuset_eff_raw[8192], weight_raw[32], stat_raw[2048];
    struct cfs_stat stat;
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

static void parse_cpu_max(const char *text, struct cg_reading *cg) {
    char quota[32], period[32], more[2], *end;
    cg->quota_max = -1;
    if (sscanf(text, "%31s %31s %1s", quota, period, more) != 2) goto invalid;
    errno = 0;
    cg->period = strtoull(period, &end, 10);
    if (!isdigit((unsigned char)*period) || errno || *end || !cg->period) goto invalid;
    if (!strcmp(quota, "max")) {
        cg->quota_max = 1;
        return;
    }
    errno = 0;
    cg->quota = strtoull(quota, &end, 10);
    if (!isdigit((unsigned char)*quota) || errno || *end || !cg->quota) goto invalid;
    cg->quota_max = 0;
    return;
invalid:
    cg->max_err = EPROTO;
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

static void *spin_fn(void *arg) {
    struct worker *w = arg;
    w->aff_start_err = pthread_getaffinity_np(pthread_self(), sizeof(w->aff_start), &w->aff_start);
    w->cpu_start = now_ns(CLOCK_THREAD_CPUTIME_ID);
    uint64_t x = 0x9e3779b97f4a7c15ull ^ (uint64_t)w->idx;
    while (now_ns(CLOCK_MONOTONIC) < spin_deadline) {
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

    uint64_t t0 = now_ns(CLOCK_MONOTONIC);
    spin_deadline = t0 + duration_ms * 1000000ull;
    struct worker *workers = calloc((size_t)threads, sizeof(*workers));
    if (!workers) { perror("calloc"); return 1; }
    pthread_t *ids = calloc((size_t)threads, sizeof(*ids));
    if (!ids) { perror("calloc"); return 1; }

    struct cg_reading cg0 = read_cg();
    uint64_t proc0 = now_ns(CLOCK_PROCESS_CPUTIME_ID);
    printf("{\"event\":\"start\",\"wall_ns\":%llu,\"proc_cpu_ns\":%llu,\"threads\":%d,\"nproc\":%ld,\"clk_tck\":%ld,",
           (unsigned long long)t0, (unsigned long long)proc0, threads,
           (long)sysconf(_SC_NPROCESSORS_ONLN), (long)sysconf(_SC_CLK_TCK));
    json_affinity("affinity", &own);
    json_cg("cgroup", &cg0);
    fputs("}\n", stdout);
    fflush(stdout);

    for (int i = 0; i < threads; i++) {
        workers[i].idx = i;
        int e = pthread_create(&ids[i], NULL, spin_fn, &workers[i]);
        if (e) { fprintf(stderr, "pthread_create: %s\n", strerror(e)); return 1; }
    }
    /* Samples include the live cpu.max value so the harness can align windows
       with externally observed `docker update` transitions without a shared
       clock. The sampler sleeps; only the worker threads offer load. */
    while (1) {
        struct timespec ts = {.tv_sec = sample_ms / 1000, .tv_nsec = (long)(sample_ms % 1000) * 1000000};
        nanosleep(&ts, NULL);
        uint64_t t = now_ns(CLOCK_MONOTONIC);
        struct cg_reading cg = read_cg();
        printf("{\"event\":\"sample\",\"wall_ns\":%llu,\"proc_cpu_ns\":%llu,\"quota_max\":%d,\"quota\":%llu,"
               "\"period\":%llu,\"max_errno\":%d,\"stat_errno\":%d,\"usage_usec\":%llu,\"nr_periods\":%llu,\"nr_throttled\":%llu,"
               "\"throttled_usec\":%llu}\n",
               (unsigned long long)t, (unsigned long long)now_ns(CLOCK_PROCESS_CPUTIME_ID),
               cg.quota_max, (unsigned long long)cg.quota, (unsigned long long)cg.period, cg.max_err, cg.stat_err,
               (unsigned long long)cg.stat.usage_usec, (unsigned long long)cg.stat.nr_periods,
               (unsigned long long)cg.stat.nr_throttled, (unsigned long long)cg.stat.throttled_usec);
        fflush(stdout);
        if (t >= spin_deadline) break;
    }
    for (int i = 0; i < threads; i++)
        if (pthread_join(ids[i], NULL)) { perror("pthread_join"); return 1; }

    uint64_t t1 = now_ns(CLOCK_MONOTONIC);
    uint64_t proc1 = now_ns(CLOCK_PROCESS_CPUTIME_ID);
    struct cg_reading cg1 = read_cg();
    uint64_t total = 0;
    fputs("{\"event\":\"end\",\"wall_ns\":", stdout);
    printf("%llu", (unsigned long long)t1);
    printf(",\"duration_ns\":%llu,\"proc_cpu_ns\":%llu,\"workers\":[",
           (unsigned long long)(t1 - t0), (unsigned long long)proc1);
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

static int cmd_info(void) {
    cpu_set_t own;
    CPU_ZERO(&own);
    if (sched_getaffinity(0, sizeof(own), &own)) { perror("sched_getaffinity"); return 1; }
    struct cg_reading cg = read_cg();
    printf("{\"event\":\"info\",\"wall_ns\":%llu,\"pid\":%ld,\"nproc_online\":%ld,\"nproc_conf\":%ld,"
           "\"clk_tck\":%ld,",
           (unsigned long long)now_ns(CLOCK_MONOTONIC), (long)getpid(),
           (long)sysconf(_SC_NPROCESSORS_ONLN), (long)sysconf(_SC_NPROCESSORS_CONF),
           (long)sysconf(_SC_CLK_TCK));
    json_affinity("affinity", &own);
    json_cg("cgroup", &cg);
    fputs("}\n", stdout);
    return 0;
}

static int cmd_snapshot(void) {
    struct cg_reading cg = read_cg();
    printf("{\"event\":\"snapshot\",\"wall_ns\":%llu,\"proc_cpu_ns\":%llu,\"pid\":%ld,",
           (unsigned long long)now_ns(CLOCK_MONOTONIC),
           (unsigned long long)now_ns(CLOCK_PROCESS_CPUTIME_ID), (long)getpid());
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
    struct cg_reading cg1;
    memset(&cg1, 0, sizeof(cg1));
    snprintf(cg1.max_raw, sizeof(cg1.max_raw), "200000 100000");
    parse_cpu_max(cg1.max_raw, &cg1);
    int max_ok = (!cg1.quota_max && cg1.quota == 200000 && cg1.period == 100000);
    struct cg_reading cg2;
    memset(&cg2, 0, sizeof(cg2));
    snprintf(cg2.max_raw, sizeof(cg2.max_raw), "max 100000");
    parse_cpu_max(cg2.max_raw, &cg2);
    int max2_ok = (cg2.quota_max == 1 && cg2.period == 100000 && !cg2.max_err);
    const char *invalid[] = {"max", "max 0", "-1 100000", "0 100000", "1 2 trailing", "x 100000",
                             "18446744073709551616 100000"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        struct cg_reading bad = {0};
        parse_cpu_max(invalid[i], &bad);
        if (!bad.max_err || bad.quota_max != -1) max2_ok = 0;
    }
    parse_stat("usage_usec 123\n", &st);
    if (st.present) stat_ok = 0;
    parse_stat("usage_usec bad\nnr_periods 1\nnr_throttled 0\nthrottled_usec 0", &st);
    if (st.present) stat_ok = 0;
    cpu_set_t own;
    CPU_ZERO(&own);
    sched_getaffinity(0, sizeof(own), &own);
    int aff_ok = CPU_COUNT(&own) > 0;
    if (!stat_ok) failures++;
    if (!max_ok || !max2_ok) failures++;
    if (!aff_ok) failures++;
    printf("{\"event\":\"selftest\",\"parse_stat\":\"%s\",\"parse_cpu_max\":\"%s\","
           "\"affinity\":\"%s\",\"status\":\"%s\"}\n",
           stat_ok ? "pass" : "fail", (max_ok && max2_ok) ? "pass" : "fail",
           aff_ok ? "pass" : "fail", failures ? "fail" : "pass");
    return failures ? 1 : 0;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: cpu-limit-probe selftest|info|snapshot|spin ...\n"); return 2; }
    if (!strcmp(argv[1], "selftest")) return cmd_selftest();
    if (!strcmp(argv[1], "info")) return cmd_info();
    if (!strcmp(argv[1], "snapshot")) return cmd_snapshot();
    if (!strcmp(argv[1], "spin")) return cmd_spin(argc, argv);
    fprintf(stderr, "unknown command: %s\n", argv[1]);
    return 2;
}
