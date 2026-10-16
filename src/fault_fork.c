/* Fork phases with dense/fragmented resident parents and isolated mmap cycles. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "fault_fork:%d: %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define FIELDS 64
#define ROWS 192
#define PAGE 4096UL
struct snapshot { uint64_t value[FIELDS]; unsigned count; };
struct row {
    const char *phase, *action;
    unsigned mib, fragmented, round;
    uint64_t ns, fork_ns, mmap_ns, touch_ns, unmap_ns, count;
    struct snapshot before, after;
};
static struct row rows[ROWS];
static unsigned nr, fields;
static char names[FIELDS][40];
static int guest;
static const char *self;

static uint64_t now(void) {
    struct timespec t; CHECK(!clock_gettime(CLOCK_MONOTONIC, &t));
    return (uint64_t)t.tv_sec * 1000000000 + (uint64_t)t.tv_nsec;
}
static uint64_t cycles(void) {
#if defined(__x86_64__)
    unsigned lo, hi;
    __asm__ volatile("lfence; rdtsc" : "=a"(lo), "=d"(hi) : : "memory");
    return ((uint64_t)hi << 32) | lo;
#else
    return 0;
#endif
}
static unsigned number(const char *s, unsigned limit) {
    char *end; errno = 0; unsigned long v = strtoul(s, &end, 10);
    CHECK(!errno && *s >= '0' && *s <= '9' && !*end && v && v <= limit);
    return (unsigned)v;
}
static void snapshot(struct snapshot *s) {
    FILE *f = fopen(guest ? "/proc/uml_fault_profile" : "/sys/fs/cgroup/cpu.stat", "r");
    if (!f) { CHECK(!guest && errno == ENOENT); return; }
    char line[160], key[40]; uint64_t v;
    while (fgets(line, sizeof(line), f)) {
        if (*line == '#') continue;
        CHECK(sscanf(line, "%39s %" SCNu64, key, &v) == 2);
        if (!strcmp(key, "enabled")) { CHECK(v == 1); continue; }
        if (!guest) {
            if (!strcmp(key, "nr_periods")) strcpy(key, "host_periods");
            else if (!strcmp(key, "nr_throttled")) strcpy(key, "host_throttled");
            else continue;
        }
        CHECK(s->count < FIELDS);
        if (fields) CHECK(!strcmp(names[s->count], key));
        else strcpy(names[s->count], key);
        s->value[s->count++] = v;
    }
    CHECK(!ferror(f) && !fclose(f));
    if (fields) CHECK(s->count == fields);
    else fields = s->count;
}
static struct row *begin(const char *phase, const char *action,
                         unsigned mib, unsigned fragmented, unsigned round) {
    CHECK(nr < ROWS);
    struct row *r = &rows[nr++];
    r->phase = phase; r->action = action; r->mib = mib;
    r->fragmented = fragmented; r->round = round; r->count = 1;
    snapshot(&r->before); r->ns = now(); return r;
}
static void end(struct row *r) {
    r->ns = now() - r->ns; snapshot(&r->after);
}
static void byte(int fd, char *value, int writing) {
    ssize_t n;
    do { n = writing ? write(fd, value, 1) : read(fd, value, 1); } while (n < 0 && errno == EINTR);
    CHECK(n == 1);
}
static size_t offset(size_t p, unsigned fragmented) {
    return (p + (fragmented ? (p / 16) * 16 : 0)) * PAGE;
}
static void heap_check(volatile unsigned char *p, size_t pages, unsigned fragmented) {
    for (size_t j = 0; j < pages; j++) CHECK(p[offset(j, fragmented)] == 0x5a);
}
static void mapping_cycles(unsigned count) {
    struct row *r = begin("mmap_cycle", "none", 0, 0, 0); r->count = count;
    for (unsigned i = 0; i < count; i++) {
        uint64_t t = now();
        volatile unsigned char *p = mmap(NULL, PAGE, PROT_READ | PROT_WRITE,
                                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        CHECK(p != MAP_FAILED); r->mmap_ns += now() - t;
        t = now(); *p = 0x5a; CHECK(*p == 0x5a && p[PAGE - 1] == 0);
        r->touch_ns += now() - t;
        t = now(); CHECK(!munmap((void *)p, PAGE)); r->unmap_ns += now() - t;
    }
    end(r);
}
static void heap_case(unsigned mib, unsigned fragmented, unsigned rounds) {
    size_t pages = (size_t)mib * 1024 * 1024 / PAGE;
    size_t length = pages * PAGE * (fragmented ? 2 : 1);
    volatile unsigned char *p = mmap(NULL, length, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p != MAP_FAILED);
    if (fragmented) {
        for (size_t j = 0; j < pages; j += 16)
            CHECK(!mprotect((void *)(p + offset(j, 1)), 16 * PAGE, PROT_READ | PROT_WRITE));
    } else CHECK(!mprotect((void *)p, length, PROT_READ | PROT_WRITE));
#ifdef MADV_NOHUGEPAGE
    if (madvise((void *)p, length, MADV_NOHUGEPAGE)) CHECK(errno == EINVAL);
#endif
    const char *actions[] = {"exit", "exec", "cow"};
    for (unsigned round = 0; round < rounds; round++) {
        struct row *r = begin("parent_write", "none", mib, fragmented, round);
        r->count = pages;
        for (size_t j = 0; j < pages; j++) p[offset(j, fragmented)] = 0x5a;
        end(r);
        for (unsigned action = 0; action < 3; action++) {
            int fd[2]; CHECK(!socketpair(AF_UNIX, SOCK_STREAM, 0, fd));
            r = begin("fork_ready", actions[action], mib, fragmented, round);
            uint64_t t = now(); pid_t child = fork(); CHECK(child >= 0);
            if (!child) {
                CHECK(!close(fd[0])); char b = 'R'; byte(fd[1], &b, 1); byte(fd[1], &b, 0);
                CHECK(b == 'G');
                if (action == 1) { execl(self, self, "child", NULL); _exit(127); }
                if (action == 2) {
                    heap_check(p, pages, fragmented);
                    for (size_t j = 0; j < pages; j += 16) p[offset(j, fragmented)] = 0xa5;
                    for (size_t j = 0; j < pages; j++)
                        CHECK(p[offset(j, fragmented)] == (j % 16 ? 0x5a : 0xa5));
                }
                _exit(0);
            }
            r->fork_ns = now() - t; CHECK(!close(fd[1]));
            char b; byte(fd[0], &b, 0); CHECK(b == 'R'); end(r);
            r = begin("child_finish", actions[action], mib, fragmented, round);
            b = 'G'; byte(fd[0], &b, 1);
            int status; pid_t waited;
            do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
            CHECK(waited == child && WIFEXITED(status) && !WEXITSTATUS(status)); end(r);
            CHECK(!close(fd[0])); heap_check(p, pages, fragmented);
        }
    }
    struct row *r = begin("heap_unmap", "none", mib, fragmented, 0);
    CHECK(!munmap((void *)p, length)); end(r);
}
int main(int argc, char **argv) {
    self = argv[0];
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    CHECK(argc <= 3);
    unsigned rounds = argc > 1 ? number(argv[1], 4) : 2;
    unsigned maximum = argc > 2 ? number(argv[2], 256) : 256;
    CHECK(maximum == 16 || maximum == 64 || maximum == 256);
    CHECK(sysconf(_SC_PAGESIZE) == (long)PAGE);
    guest = access("/proc/uml_fault_profile", F_OK) == 0;
    memset(rows, 0, sizeof(rows));
    uint64_t t = now(), c = cycles();
    mapping_cycles(2000);
    for (unsigned mib = 16; mib <= maximum; mib *= 4)
        for (unsigned fragmented = 0; fragmented < 2; fragmented++) heap_case(mib, fragmented, rounds);
    double hz = (cycles() - c) * 1e9 / (now() - t);
    printf("{\"schema\":\"hd.fault-fork/1\",\"verified\":true,\"tsc_hz\":%.0f,\"samples\":[", hz);
    for (unsigned i = 0; i < nr; i++) {
        struct row *r = &rows[i];
        printf("%s{\"phase\":\"%s\",\"action\":\"%s\",\"mib\":%u,\"fragmented\":%u,\"round\":%u,"
               "\"ns\":%" PRIu64 ",\"fork_ns\":%" PRIu64 ",\"count\":%" PRIu64 ","
               "\"mmap_ns\":%" PRIu64 ",\"touch_ns\":%" PRIu64 ",\"unmap_ns\":%" PRIu64 ",\"counters\":{",
               i ? "," : "", r->phase, r->action, r->mib, r->fragmented, r->round,
               r->ns, r->fork_ns, r->count, r->mmap_ns, r->touch_ns, r->unmap_ns);
        CHECK(r->before.count == r->after.count);
        for (unsigned j = 0; j < fields; j++) {
            CHECK(r->after.value[j] >= r->before.value[j]);
            printf("%s\"%s\":%" PRIu64, j ? "," : "", names[j], r->after.value[j] - r->before.value[j]);
        }
        printf("}}");
    }
    puts("]}"); return 0;
}
