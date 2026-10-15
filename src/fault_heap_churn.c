/* ./hd probe src/fault_heap_churn.c [intervals [seconds [chunk_mib]]]
 * Retain a growing sparse heap while repeatedly forking, mapping and touching.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "fault_heap_churn:%d: %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define MAX_INTERVALS 32
static const char *self;
static size_t page;
static unsigned char residency[65536];
struct sample { double elapsed, seconds; uint64_t count, exec_ns, mmap_ns, dense_ns; size_t resident; };
static struct sample samples[MAX_INTERVALS];

static uint64_t now(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &ts));
    return (uint64_t)ts.tv_sec * 1000000000 + (uint64_t)ts.tv_nsec;
}

static unsigned number(const char *text, unsigned limit) {
    char *end; errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    CHECK(!errno && *text >= '0' && *text <= '9' && !*end && value && value <= limit);
    return (unsigned)value;
}

static volatile unsigned char *allocate(size_t bytes) {
    void *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p != MAP_FAILED);
    return p;
}

static void exec_once(void) {
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) { execl(self, self, "child", NULL); _exit(127); }
    int status; pid_t ret;
    do { ret = waitpid(child, &status, 0); } while (ret < 0 && errno == EINTR);
    CHECK(ret == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

static void report(unsigned i, unsigned chunk, const struct sample *s) {
    printf("{\"elapsed\":%.3f,\"seconds\":%.3f,\"retained_mib\":%u,"
           "\"resident_pages\":%zu,\"rounds\":%" PRIu64 ",\"rates\":{"
           "\"exec\":%.3f,\"mmap\":%.3f,\"dense\":%.3f}}",
           s->elapsed, s->seconds, (i + 1) * chunk, s->resident, s->count,
           s->count * 1e9 / s->exec_ns, s->count * 64e9 / s->mmap_ns,
           s->count * 256e9 / s->dense_ns);
}

int main(int argc, char **argv) {
    page = (size_t)sysconf(_SC_PAGESIZE); CHECK(page && page <= 65536);
    self = argv[0];
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    CHECK(argc <= 4);
    unsigned intervals = argc > 1 ? number(argv[1], MAX_INTERVALS) : 12;
    unsigned seconds = argc > 2 ? number(argv[2], 60) : 15;
    unsigned chunk = argc > 3 ? number(argv[3], 64) : 32;
    size_t bytes = (size_t)chunk * 1024 * 1024, pages = bytes / page;
    CHECK(pages && pages <= sizeof(residency));
    volatile unsigned char *raw[MAX_INTERVALS], *heap[MAX_INTERVALS];
    size_t window = page * 32;
    setvbuf(stdout, NULL, _IOLBF, 0);
    uint64_t begin = now();
    for (unsigned i = 0; i < intervals; i++) {
        raw[i] = mmap(NULL, bytes + 2 * window, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        CHECK(raw[i] != MAP_FAILED);
        heap[i] = (void *)(((uintptr_t)raw[i] + 2 * window - 1) & ~(uintptr_t)(window - 1));
        CHECK(!mprotect((void *)heap[i], bytes, PROT_READ | PROT_WRITE));
#ifdef MADV_NOHUGEPAGE
        if (madvise((void *)heap[i], bytes, MADV_NOHUGEPAGE)) CHECK(errno == EINVAL);
#endif
        for (size_t j = 0; j < pages; j += 32) heap[i][j * page] = (unsigned char)(i + 1);
        struct sample *s = &samples[i];
        for (unsigned k = 0; k <= i; k++) {
            CHECK(!mincore((void *)heap[k], bytes, (void *)residency));
            for (size_t j = 0; j < pages; j++) s->resident += !!(residency[j] & 1);
            for (size_t j = 0; j < pages; j += 32) CHECK(heap[k][j * page] == (unsigned char)(k + 1));
        }
        uint64_t start = now(), end;
        do {
            uint64_t t = now(); exec_once();
            s->exec_ns += now() - t;
            t = now();
            for (unsigned k = 0; k < 64; k++) {
                volatile unsigned char *p = allocate(page);
                *p = (unsigned char)k; CHECK(*p == (unsigned char)k);
                CHECK(!munmap((void *)p, page));
            }
            s->mmap_ns += now() - t;
            t = now();
            volatile unsigned char *p = allocate(256 * page);
            for (size_t k = 0; k < 256; k++) p[k * page] = (unsigned char)k;
            for (size_t k = 0; k < 256; k++) CHECK(p[k * page] == (unsigned char)k);
            CHECK(!munmap((void *)p, 256 * page));
            s->dense_ns += now() - t;
            s->count++; end = now();
        } while (end - start < (uint64_t)seconds * 1000000000);
        s->elapsed = (end - begin) / 1e9; s->seconds = (end - start) / 1e9;
        report(i, chunk, s); putchar('\n');
    }
    for (unsigned i = 0; i < intervals; i++) CHECK(!munmap((void *)raw[i], bytes + 2 * window));
    printf("{\"schema\":\"hd.fault-heap-churn/1\",\"verified\":true,\"page_bytes\":%zu,\"samples\":[", page);
    for (unsigned i = 0; i < intervals; i++) { if (i) putchar(','); report(i, chunk, &samples[i]); }
    puts("]}");
    return 0;
}
