/* ./hd probe src/fault_churn.c [seconds [interval [dense_pages]]]
 * Fixed batches mix exec, small mmap and fresh dense/sparse anonymous faults.
 * The final JSON retains every interval even when the driver truncates stdout.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "fault_churn:%d: %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define MAX_SAMPLES 1024
#define KINDS 4
static const char *names[KINDS] = {"exec", "mmap", "dense", "sparse"};
static const char *self;
static size_t page;

struct sample {
    double elapsed, seconds;
    uint64_t count[KINDS], ns[KINDS];
    long minflt, child_minflt, maps;
    long available, anon, slab, pagetables, pgscan, pgsteal;
};
static struct sample samples[MAX_SAMPLES];

static uint64_t now_ns(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &ts));
    return (uint64_t)ts.tv_sec * 1000000000 + (uint64_t)ts.tv_nsec;
}

static unsigned long number(const char *s, unsigned long limit) {
    char *end;
    errno = 0;
    unsigned long value = strtoul(s, &end, 10);
    CHECK(!errno && *s >= '0' && *s <= '9' && !*end && value && value <= limit);
    return value;
}

static unsigned char *allocate(size_t bytes) {
    void *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p != MAP_FAILED);
    return p;
}

static void child_ok(pid_t child) {
    int status;
    pid_t ret;
    do { ret = waitpid(child, &status, 0); } while (ret < 0 && errno == EINTR);
    CHECK(ret == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

static void exec_batch(void) {
    for (int i = 0; i < 8; i++) {
        pid_t child = fork();
        CHECK(child >= 0);
        if (!child) {
            execl(self, self, "child", NULL);
            _exit(127);
        }
        child_ok(child);
    }
}

static void mmap_batch(void) {
    for (unsigned int i = 0; i < 256; i++) {
        volatile unsigned char *p = allocate(page);
        p[0] = (unsigned char)i;
        CHECK(p[0] == (unsigned char)i);
        CHECK(!munmap((void *)p, page));
    }
}

static void touch_batch(size_t pages, size_t stride) {
    size_t bytes = pages * page;
    volatile unsigned char *p = allocate(bytes);
    for (size_t i = 0; i < pages; i += stride) p[i * page] = (unsigned char)(i % 251 + 1);
    for (size_t i = 0; i < pages; i += stride) CHECK(p[i * page] == (unsigned char)(i % 251 + 1));
    CHECK(!munmap((void *)p, bytes));
}

static long field(const char *path, const char *key) {
    FILE *f = fopen(path, "r");
    CHECK(f);
    char line[256], name[96];
    long value, found = -1;
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "%95s %ld", name, &value) == 2 && !strcmp(name, key)) {
            found = value;
            break;
        }
    }
    CHECK(!ferror(f) && !fclose(f));
    return found;
}

static void snapshot(struct sample *s) {
    FILE *f = fopen("/proc/self/maps", "r");
    CHECK(f);
    int c;
    while ((c = fgetc(f)) != EOF) s->maps += c == '\n';
    CHECK(!ferror(f) && !fclose(f));
    s->available = field("/proc/meminfo", "MemAvailable:");
    s->anon = field("/proc/meminfo", "AnonPages:");
    s->slab = field("/proc/meminfo", "Slab:");
    s->pagetables = field("/proc/meminfo", "PageTables:");
    s->pgscan = field("/proc/vmstat", "pgscan_kswapd");
    s->pgsteal = field("/proc/vmstat", "pgsteal_kswapd");
}

static void report(const struct sample *s) {
    printf("{\"elapsed\":%.3f,\"seconds\":%.3f,\"rates\":{", s->elapsed, s->seconds);
    for (int k = 0; k < KINDS; k++)
        printf("%s\"%s\":%.3f", k ? "," : "", names[k], s->ns[k] ? s->count[k] * 1e9 / s->ns[k] : 0);
    printf("},\"counts\":[");
    for (int k = 0; k < KINDS; k++) printf("%s%" PRIu64, k ? "," : "", s->count[k]);
    printf("],\"work_ns\":[");
    for (int k = 0; k < KINDS; k++) printf("%s%" PRIu64, k ? "," : "", s->ns[k]);
    printf("],\"minor_faults\":%ld,\"child_minor_faults\":%ld,\"self_maps\":%ld,"
           "\"mem_available_kb\":%ld,\"anon_kb\":%ld,\"slab_kb\":%ld,\"pagetables_kb\":%ld,"
           "\"pgscan_kswapd\":%ld,\"pgsteal_kswapd\":%ld}", s->minflt, s->child_minflt, s->maps,
           s->available, s->anon, s->slab, s->pagetables, s->pgscan, s->pgsteal);
}

int main(int argc, char **argv) {
    page = (size_t)sysconf(_SC_PAGESIZE);
    CHECK(page && page < 65536);
    self = argv[0];
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    CHECK(argc <= 4);
    unsigned long seconds = argc > 1 ? number(argv[1], 7200) : 180;
    unsigned long interval = argc > 2 ? number(argv[2], 600) : 15;
    size_t pages = argc > 3 ? number(argv[3], 65536) : 4096;
    CHECK((seconds + interval - 1) / interval <= MAX_SAMPLES);
    setvbuf(stdout, NULL, _IOLBF, 0);
    uint64_t begin = now_ns(), deadline = begin + seconds * 1000000000ULL;
    unsigned int n = 0;
    struct sample initial = {0};
    snapshot(&initial);
    uint64_t end = begin;
    while (end < deadline) {
        CHECK(n < MAX_SAMPLES);
        struct sample *s = samples + n++;
        struct rusage before, after, cbefore, cafter;
        CHECK(!getrusage(RUSAGE_SELF, &before) && !getrusage(RUSAGE_CHILDREN, &cbefore));
        uint64_t start = now_ns(), until = start + interval * 1000000000ULL;
        if (until > deadline) until = deadline;
        do {
            for (int k = 0; k < KINDS; k++) {
                uint64_t first = now_ns();
                if (k == 0) { exec_batch(); s->count[k] += 8; }
                if (k == 1) { mmap_batch(); s->count[k] += 256; }
                if (k == 2) { touch_batch(pages, 1); s->count[k] += pages; }
                if (k == 3) { touch_batch(pages, 32); s->count[k] += (pages + 31) / 32; }
                s->ns[k] += now_ns() - first;
            }
            end = now_ns();
        } while (end < until);
        CHECK(!getrusage(RUSAGE_SELF, &after) && !getrusage(RUSAGE_CHILDREN, &cafter));
        s->seconds = (end - start) / 1e9;
        s->elapsed = (end - begin) / 1e9;
        s->minflt = after.ru_minflt - before.ru_minflt;
        s->child_minflt = cafter.ru_minflt - cbefore.ru_minflt;
        snapshot(s);
        report(s);
        putchar('\n');
    }
    printf("{\"schema\":\"hd.fault-churn/1\",\"verified\":true,\"dense_pages\":%zu,"
           "\"page_bytes\":%zu,\"seconds\":%lu,\"interval\":%lu,\"initial\":", pages, page, seconds, interval);
    report(&initial);
    printf(",\"samples\":[");
    for (unsigned int i = 0; i < n; i++) { if (i) putchar(','); report(samples + i); }
    puts("]}");
    return 0;
}
