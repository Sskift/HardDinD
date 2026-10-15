/* Fixed work counts; clocks and optional UML counters stay outside hot loops. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core failure line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define MAX_COUNTERS 32
static size_t page;
static volatile uint64_t sink;
static const char *self;
static int grouped, group_items;
static sigjmp_buf recovery;
static volatile sig_atomic_t expected, caught;

struct counters {
    char names[MAX_COUNTERS][40];
    uint64_t values[MAX_COUNTERS];
    unsigned int count;
    int enabled;
};
struct sample {
    uint64_t started, ns;
    struct rusage before, after;
    struct counters first, last;
};

static uint64_t now_ns(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &ts));
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
static void counters(struct counters *out) {
    FILE *f = fopen("/proc/uml_faultstat", "r");
    memset(out, 0, sizeof(*out));
    if (!f) {
        CHECK(errno == ENOENT); /* Permission errors must not look like absent counters. */
        return;
    }
    char line[256], name[40];
    uint64_t value;
    while (fgets(line, sizeof(line), f)) {
        if (line[0] == '#') continue;
        CHECK(sscanf(line, "%39s %" SCNu64, name, &value) == 2);
        if (!strcmp(name, "enabled")) { out->enabled = (int)value; continue; }
        CHECK(out->count < MAX_COUNTERS);
        strcpy(out->names[out->count], name);
        out->values[out->count++] = value;
    }
    CHECK(!ferror(f) && !fclose(f));
}
static void begin(struct sample *s) {
    counters(&s->first);
    CHECK(!getrusage(RUSAGE_SELF, &s->before));
    s->started = now_ns();
}
static void end(struct sample *s) {
    s->ns = now_ns() - s->started;
    CHECK(!getrusage(RUSAGE_SELF, &s->after));
    counters(&s->last);
}
static void report(const char *mode, size_t count, const struct sample *s) {
    CHECK(s->ns > 0 && s->first.count == s->last.count);
    if (grouped && group_items++) putchar(',');
    printf("{\"schema\":\"hd.core/1\",\"mode\":\"%s\",\"operations\":%zu,"
           "\"seconds\":%.9f,\"ns_per_op\":%.3f,\"minor_faults\":%ld,"
           "\"major_faults\":%ld,\"fault_scope\":\"self\",\"counter_scope\":\"machine\",\"counters_enabled\":%d,"
           "\"counters\":{", mode, count, s->ns / 1e9, (double)s->ns / count,
           s->after.ru_minflt - s->before.ru_minflt,
           s->after.ru_majflt - s->before.ru_majflt, s->last.enabled);
    for (unsigned int i = 0; i < s->first.count; i++) {
        CHECK(!strcmp(s->first.names[i], s->last.names[i]));
        CHECK(s->last.values[i] >= s->first.values[i]);
        printf("%s\"%s\":%" PRIu64, i ? "," : "", s->first.names[i],
               s->last.values[i] - s->first.values[i]);
    }
    printf("}}%s", grouped ? "" : "\n");
}
static void child_ok(pid_t child) {
    int status;
    pid_t ret;
    do { ret = waitpid(child, &status, 0); } while (ret < 0 && errno == EINTR);
    CHECK(ret == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
}
static unsigned char pattern(size_t i) { return (unsigned char)(i % 251 + 1); }
static unsigned char *anonymous(size_t bytes) {
    /* Align the VMA so faultaround's window containment is deterministic. */
    const size_t alignment = 2UL * 1024 * 1024;
    unsigned char *raw = mmap(NULL, bytes + alignment, PROT_READ | PROT_WRITE,
                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(raw != MAP_FAILED);
    uintptr_t addr = ((uintptr_t)raw + alignment - 1) & ~(alignment - 1);
    size_t prefix = addr - (uintptr_t)raw;
    if (prefix) CHECK(!munmap(raw, prefix));
    CHECK(!munmap((void *)(addr + bytes), alignment - prefix));
    /* Compare 4 KiB faults on both systems, not native THP against UML PTEs. */
    int advice = madvise((void *)addr, bytes, MADV_NOHUGEPAGE);
    CHECK(!advice || errno == EINVAL); /* UML without CONFIG_TRANSPARENT_HUGEPAGE. */
    return (void *)addr;
}
static int backing(size_t pages) {
    char name[] = "core-faults-XXXXXX";
    int fd = mkstemp(name);
    CHECK(fd >= 0 && !unlink(name));
    unsigned char *buf = calloc(1, page);
    CHECK(buf != NULL);
    for (size_t i = 0; i < pages; i++) {
        buf[0] = pattern(i);
        CHECK(write(fd, buf, page) == (ssize_t)page);
    }
    CHECK(!fsync(fd));
    free(buf);
    return fd;
}
static void fault(int sig) {
    if (!expected) _exit(128 + sig);
    caught = sig;
    siglongjmp(recovery, 1);
}
static void denied(volatile unsigned char *p, int write_access, int sig) {
    expected = sig;
    caught = 0;
    if (!sigsetjmp(recovery, 1)) {
        if (write_access) *p = 99;
        else sink = *p;
    }
    expected = 0;
    CHECK(caught == sig);
}
static void semantics(void) {
    const size_t pages = 513, bytes = pages * page;
    struct sigaction sa = {.sa_handler = fault};
    sigemptyset(&sa.sa_mask);
    CHECK(!sigaction(SIGSEGV, &sa, NULL) && !sigaction(SIGBUS, &sa, NULL));
    unsigned char *p = anonymous(bytes);
    /* Protect neighbours before any fault; test partial windows and PMD edges. */
    CHECK(!mprotect(p + 31 * page, page, PROT_NONE));
    p[30 * page + 17] = 0x71;
    p[32 * page + 19] = 0x73;
    denied(p + 31 * page, 0, SIGSEGV);
    denied(p + 31 * page, 1, SIGSEGV);
    CHECK(!mprotect(p + 31 * page, page, PROT_READ | PROT_WRITE));
    CHECK(p[31 * page] == 0 && p[30 * page + 17] == 0x71 && p[32 * page + 19] == 0x73);
    for (size_t i = 0; i < pages; i++) {
        CHECK(p[i * page] == 0);
        p[i * page] = pattern(i);
    }
    pid_t child = fork();
    CHECK(child >= 0);
    if (!child) {
        for (size_t i = 0; i < pages; i++) {
            CHECK(p[i * page] == pattern(i));
            p[i * page] = 0xa5;
        }
        CHECK(!madvise(p + page, (pages - 2) * page, MADV_DONTNEED));
        for (size_t i = 1; i + 1 < pages; i++) CHECK(p[i * page] == 0);
        CHECK(p[0] == 0xa5 && p[(pages - 1) * page] == 0xa5);
        _exit(0);
    }
    child_ok(child);
    for (size_t i = 0; i < pages; i++) CHECK(p[i * page] == pattern(i));
    CHECK(!mprotect(p, bytes, PROT_READ));
    denied(p + 7 * page, 1, SIGSEGV);
    CHECK(!madvise(p, bytes, MADV_DONTNEED));
    for (size_t i = 0; i < pages; i++) CHECK(p[i * page] == 0);
    CHECK(!munmap(p, bytes));
    puts("CORE_CHECK anonymous_cow_protection_discard pass");

    int fd = backing(pages);
    p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, 0);
    CHECK(p != MAP_FAILED);
    unsigned char *shared = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    CHECK(shared != MAP_FAILED);
    for (size_t i = 0; i < pages; i++) {
        CHECK(p[i * page] == pattern(i) && shared[i * page] == pattern(i));
        p[i * page] = 0xa5;
    }
    for (size_t i = 0; i < pages; i++) CHECK(shared[i * page] == pattern(i));
    CHECK(!madvise(p, bytes, MADV_DONTNEED));
    for (size_t i = 0; i < pages; i++) CHECK(p[i * page] == pattern(i));
    for (size_t i = 0; i < pages; i++) shared[i * page] = 0x39;
    CHECK(!msync(shared, bytes, MS_SYNC));
    for (size_t i = 0; i < pages; i++) {
        unsigned char byte;
        CHECK(pread(fd, &byte, 1, (off_t)(i * page)) == 1 && byte == 0x39);
    }
    CHECK(!ftruncate(fd, 0));
    denied(shared + 32 * page, 0, SIGBUS);
    CHECK(!munmap(p, bytes) && !munmap(shared, bytes) && !close(fd));
    p = anonymous(bytes);
    memset(p, 0x7a, bytes);
    CHECK(mmap(p, bytes, PROT_READ | PROT_WRITE, MAP_FIXED | MAP_PRIVATE | MAP_ANONYMOUS,
               -1, 0) == p);
    for (size_t i = 0; i < bytes; i++) CHECK(p[i] == 0);
    CHECK(!munmap(p, bytes));
    puts("CORE_CHECK file_cow_dirty_truncate_fixed pass");
    puts("CORE_SEMANTICS_END status=0");
    puts("{\"schema\":\"hd.core/1\",\"mode\":\"semantics\",\"verified\":true}");
}
static void measure(const char *mode, size_t count) {
    struct sample local = {0}, *s = &local;
    unsigned char *p = NULL;
    size_t bytes = count * page;
    int fd = -1;
    if (!strcmp(mode, "anon") || !strcmp(mode, "cow")) p = anonymous(bytes);
    if (!strcmp(mode, "file")) {
        fd = backing(count); /* Warm page cache; measure PTE faults, not storage. */
        p = mmap(NULL, bytes, PROT_READ, MAP_PRIVATE, fd, 0);
        CHECK(p != MAP_FAILED);
    }
    if (!strcmp(mode, "cow")) {
        for (size_t i = 0; i < count; i++) p[i * page] = pattern(i);
        s = mmap(NULL, sizeof(*s), PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
        CHECK(s != MAP_FAILED);
        memset(s, 0, sizeof(*s));
        pid_t child = fork();
        CHECK(child >= 0);
        if (!child) {
            begin(s);
            for (size_t i = 0; i < count; i++) p[i * page] = 0xa5;
            end(s);
            for (size_t i = 0; i < count; i++) CHECK(p[i * page] == 0xa5);
            _exit(0);
        }
        child_ok(child);
        for (size_t i = 0; i < count; i++) CHECK(p[i * page] == pattern(i));
    } else {
        if (!strcmp(mode, "mprotect")) { p = anonymous(page); p[0] = 0x53; }
        begin(s);
        if (!strcmp(mode, "anon")) {
            for (size_t i = 0; i < count; i++) p[i * page] = pattern(i);
        } else if (!strcmp(mode, "file")) {
            uint64_t sum = 0;
            for (size_t i = 0; i < count; i++) sum += p[i * page];
            sink = sum;
        } else if (!strcmp(mode, "fork") || !strcmp(mode, "exec")) {
            for (size_t i = 0; i < count; i++) {
                pid_t child = fork();
                CHECK(child >= 0);
                if (!child) {
                    if (!strcmp(mode, "exec")) { execl(self, self, "child", NULL); _exit(127); }
                    _exit(0);
                }
                child_ok(child);
            }
        } else if (!strcmp(mode, "mmap")) {
            for (size_t i = 0; i < count; i++) {
                void *q = mmap(NULL, 32 * page, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                CHECK(q != MAP_FAILED && !munmap(q, 32 * page));
            }
        } else if (!strcmp(mode, "small")) {
            for (size_t i = 0; i < count; i++) {
                unsigned char *q = mmap(NULL, 18 * page, PROT_NONE,
                                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                CHECK(q != MAP_FAILED);
                CHECK(!mprotect(q + page, 16 * page, PROT_READ | PROT_WRITE));
                for (size_t j = 1; j <= 16; j++) q[j * page] = pattern(j);
                for (size_t j = 1; j <= 16; j++) CHECK(q[j * page] == pattern(j));
                CHECK(!munmap(q, 18 * page));
            }
        } else if (!strcmp(mode, "mprotect")) {
            for (size_t i = 0; i < count; i++) {
                CHECK(!mprotect(p, page, PROT_READ));
                CHECK(!mprotect(p, page, PROT_READ | PROT_WRITE));
            }
        } else if (!strcmp(mode, "getpid")) {
            for (size_t i = 0; i < count; i++) CHECK(syscall(SYS_getpid) > 0);
        } else { fprintf(stderr, "unknown mode: %s\n", mode); exit(2); }
        end(s);
    }
    if (!strcmp(mode, "anon") || !strcmp(mode, "file"))
        for (size_t i = 0; i < count; i++) {
            CHECK(p[i * page] == pattern(i));
            CHECK(p[i * page + page - 1] == 0);
        }
    if (!strcmp(mode, "mprotect")) { CHECK(p[0] == 0x53); bytes = page; }
    report(mode, count, s);
    if (p) CHECK(!munmap(p, bytes));
    if (fd >= 0) CHECK(!close(fd));
    if (s != &local) CHECK(!munmap(s, sizeof(*s)));
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    if (argc < 2 || argc > 3) {
        fprintf(stderr, "usage: %s semantics | anon|file|cow|small|fork|exec|mmap|mprotect|getpid|faults|process|all [count]\n", argv[0]);
        return 2;
    }
    page = (size_t)sysconf(_SC_PAGESIZE);
    CHECK(page == 4096); /* Explicitly comparable 4 KiB work units. */
    self = argv[0];
    if (!strcmp(argv[1], "semantics")) { semantics(); return 0; }
    size_t count = 1024;
    if (argc == 3) {
        char *tail;
        errno = 0;
        unsigned long value = strtoul(argv[2], &tail, 10);
        CHECK(!errno && *argv[2] && !*tail && value > 0 && value <= 1048576);
        count = value;
    }
    if (!strcmp(argv[1], "faults") || !strcmp(argv[1], "process") || !strcmp(argv[1], "all")) {
        int all = !strcmp(argv[1], "all");
        if (all || !strcmp(argv[1], "process")) CHECK(count <= 1024);
        grouped = 1;
        printf("{\"schema\":\"hd.core-group/1\",\"samples\":[");
        if (all || !strcmp(argv[1], "faults")) {
            size_t pages = all ? count * 256 : count;
            measure("anon", pages);
            measure("file", pages);
            measure("cow", pages);
        }
        if (all || !strcmp(argv[1], "process")) {
            if (all) measure("small", count * 4);
            measure("fork", count);
            measure("exec", count);
            measure("mmap", count * 32);
            measure("mprotect", count * 32);
            measure("getpid", count * 2048);
        }
        puts("]}");
    } else measure(argv[1], count);
    return 0;
}
