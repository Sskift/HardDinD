/* Fault wall time, nested kernel counters, and native host-path calibrations. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <time.h>
#ifdef __linux__
#include <ucontext.h>
#endif
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "fault_costs:%d: %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define MAX_FIELDS 64
#define MAX_SAMPLES 18
struct fields { char name[MAX_FIELDS][40]; uint64_t value[MAX_FIELDS]; unsigned count; };
struct sample { const char *name; size_t count; uint64_t ns; long faults; struct fields before, after; };
static struct sample samples[MAX_SAMPLES];
static unsigned used;
static size_t page;
static int guest, profile_enabled;

static uint64_t now(void) {
    struct timespec t; CHECK(!clock_gettime(CLOCK_MONOTONIC, &t));
    return (uint64_t)t.tv_sec * 1000000000 + (uint64_t)t.tv_nsec;
}
static uint64_t cycles(void) {
#if defined(__linux__) && defined(__x86_64__)
    unsigned lo, hi;
    __asm__ volatile("lfence; rdtsc" : "=a"(lo), "=d"(hi) : : "memory");
    return ((uint64_t)hi << 32) | lo;
#else
    return 0;
#endif
}
static void snapshot(struct fields *out) {
    const char *path = guest ? "/proc/uml_fault_profile" : "/sys/fs/cgroup/cpu.stat";
    FILE *f = fopen(path, "r");
    if (!f) { CHECK(!guest && errno == ENOENT); return; }
    char line[160], name[40]; uint64_t value;
    while (fgets(line, sizeof(line), f)) {
        if (line[0] == '#') continue;
        CHECK(sscanf(line, "%39s %" SCNu64, name, &value) == 2);
        if (!guest) {
            if (!strcmp(name, "nr_periods")) strcpy(name, "host_periods");
            else if (!strcmp(name, "nr_throttled")) strcpy(name, "host_throttled");
            else continue;
        }
        if (!strcmp(name, "enabled")) { CHECK(value <= 1); profile_enabled = (int)value; continue; }
        CHECK(out->count < MAX_FIELDS);
        strcpy(out->name[out->count], name); out->value[out->count++] = value;
    }
    CHECK(!ferror(f)); CHECK(!fclose(f));
}
static struct sample *begin(const char *name, size_t count) {
    CHECK(used < MAX_SAMPLES);
    struct sample *s = &samples[used++];
    s->name = name; s->count = count;
    snapshot(&s->before);
    struct rusage r; CHECK(!getrusage(RUSAGE_SELF, &r)); s->faults = r.ru_minflt;
    s->ns = now(); return s;
}
static void end(struct sample *s) {
    s->ns = now() - s->ns;
    struct rusage r; CHECK(!getrusage(RUSAGE_SELF, &r)); s->faults = r.ru_minflt - s->faults;
    snapshot(&s->after); CHECK(s->after.count == s->before.count);
}
static unsigned char *map(size_t bytes, int fd, int prot) {
    void *p = mmap(NULL, bytes, prot, fd < 0 ? MAP_PRIVATE | MAP_ANONYMOUS : MAP_SHARED, fd, 0);
    CHECK(p != MAP_FAILED);
#ifdef MADV_NOHUGEPAGE
    if (madvise(p, bytes, MADV_NOHUGEPAGE)) CHECK(errno == EINVAL);
#endif
    return p;
}
static void touch(volatile unsigned char *p, size_t pages) {
    for (size_t i = 0; i < pages; i++) p[i * page] = (unsigned char)(i % 251 + 1);
}
static void verify(volatile unsigned char *p, size_t pages) {
    for (size_t i = 0; i < pages; i++) {
        CHECK(p[i * page] == (unsigned char)(i % 251 + 1));
        CHECK(p[i * page + page - 1] == 0);
    }
}
#if defined(__linux__) && defined(__x86_64__)
static volatile sig_atomic_t caught;
static void segv(int sig, siginfo_t *info, void *context) {
    (void)info;
    if (sig != SIGSEGV) _exit(99);
    ((ucontext_t *)context)->uc_mcontext.gregs[REG_RIP] += 2; /* movb (%rdi), %al */
    caught++;
}
static void host_calibration(size_t pages) {
    size_t bytes = pages * page;
    unsigned char *bad = map(page, -1, PROT_NONE);
    struct sigaction sa = {.sa_sigaction = segv, .sa_flags = SA_SIGINFO}, old;
    sigemptyset(&sa.sa_mask); CHECK(!sigaction(SIGSEGV, &sa, &old));
    const unsigned faults = 20000;
    struct sample *s = begin("host_sigsegv_return", faults);
    for (unsigned i = 0; i < faults; i++)
        __asm__ volatile("movb (%%rdi), %%al" : : "D"(bad) : "rax", "memory");
    end(s); CHECK(caught == (sig_atomic_t)faults);
    CHECK(!sigaction(SIGSEGV, &old, NULL)); CHECK(!munmap(bad, page));

    int fd = (int)syscall(SYS_memfd_create, "fault-costs", 0); CHECK(fd >= 0);
    CHECK(!ftruncate(fd, (off_t)bytes));
    unsigned char *ram = map(bytes, fd, PROT_READ | PROT_WRITE);
    s = begin("host_memfd_zero", pages); memset(ram, 0, bytes); end(s);
    touch(ram, pages); /* Backing pages already resident for alias calibration. */
    const unsigned steps[] = {1, 32, 512, 32, 512};
    const char *names[] = {"host_mmap_1", "host_mmap_32", "host_mmap_512", "host_populate_32", "host_populate_512"};
    const char *fault_names[] = {"host_alias_fault_1", "host_alias_fault_32", "host_alias_fault_512", "host_populated_touch_32", "host_populated_touch_512"};
    for (unsigned k = 0; k < 5; k++) {
        unsigned char *p = map(bytes, -1, PROT_NONE);
        unsigned step = steps[k];
        s = begin(names[k], pages / step);
        for (size_t i = 0; i < pages; i += step)
            CHECK(mmap(p + i * page, step * page, PROT_READ | PROT_WRITE,
                       MAP_SHARED | MAP_FIXED | (k >= 3 ? MAP_POPULATE : 0),
                       fd, (off_t)(i * page)) == p + i * page);
        end(s);
        s = begin(fault_names[k], pages); touch(p, pages); end(s);
        verify(p, pages); CHECK(!munmap(p, bytes));
    }
    CHECK(!munmap(ram, bytes)); CHECK(!close(fd));
}
#endif
int main(int argc, char **argv) {
    CHECK(argc <= 2);
    size_t pages = 65536;
    if (argc == 2) {
        char *tail; errno = 0; unsigned long value = strtoul(argv[1], &tail, 10);
        CHECK(!errno && *argv[1] >= '0' && *argv[1] <= '9' && !*tail && value >= 512 && value <= 262144 && value % 512 == 0);
        pages = value;
    }
    page = (size_t)sysconf(_SC_PAGESIZE); CHECK(page == 4096);
    guest = access("/proc/uml_fault_profile", F_OK) == 0;
    uint64_t clock_start = now(), tsc_start = cycles();
    size_t bytes = pages * page;
    unsigned char *p = map(bytes, -1, PROT_READ | PROT_WRITE);
    memset(samples, 0, sizeof(samples)); /* Keep snapshot storage faults out of timing. */
    struct sample *s = begin("anon_first", pages); touch(p, pages); end(s);
    s = begin("anon_verify", pages); verify(p, pages); end(s);
    s = begin("anon_repeat", pages); touch(p, pages); end(s); verify(p, pages);
    memset(p, 0x7a, bytes);
    s = begin("resident_zero", pages); memset(p, 0, bytes); end(s);
    for (size_t i = 0; i < bytes; i++) CHECK(p[i] == 0);
    s = begin("anon_release", pages); CHECK(!munmap(p, bytes)); end(s);
    s = begin("anon_cycles", pages * 8);
    for (unsigned i = 0; i < 8; i++) {
        p = map(bytes, -1, PROT_READ | PROT_WRITE);
        touch(p, pages); verify(p, pages); CHECK(!munmap(p, bytes));
    }
    end(s);
#if defined(__linux__) && defined(__x86_64__)
    if (!guest) host_calibration(pages);
#endif
    uint64_t clock_end = now(), tsc_end = cycles();
    double tsc_hz = (tsc_end - tsc_start) * 1e9 / (clock_end - clock_start);
    printf("{\"schema\":\"hd.fault-costs/1\",\"verified\":true,\"page_bytes\":%zu,\"profile\":%s,\"profile_enabled\":%d,\"tsc_hz\":%.0f,\"samples\":[", page, guest ? "true" : "false", profile_enabled, tsc_hz);
    for (unsigned i = 0; i < used; i++) {
        s = &samples[i];
        printf("%s{\"name\":\"%s\",\"operations\":%zu,\"ns_per_op\":%.3f,\"seconds\":%.9f,\"minor_faults\":%ld,\"counters\":{",
               i ? "," : "", s->name, s->count, (double)s->ns / s->count, s->ns / 1e9, s->faults);
        for (unsigned j = 0; j < s->before.count; j++) {
            CHECK(!strcmp(s->before.name[j], s->after.name[j]));
            CHECK(s->after.value[j] >= s->before.value[j]);
            printf("%s\"%s\":%" PRIu64, j ? "," : "", s->before.name[j], s->after.value[j] - s->before.value[j]);
        }
        printf("}}");
    }
    puts("]}"); return 0;
}
