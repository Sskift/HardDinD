/* Large-window zero-fill, guard, COW, discard, sparse-residency and rlimit checks. */
#define _GNU_SOURCE
#include <errno.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "fault_window:%d: %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
static sigjmp_buf recovery;
static volatile sig_atomic_t expected, caught;
static unsigned guards;
static void handler(int sig) {
    if (!expected) _exit(128 + sig);
    caught = sig; siglongjmp(recovery, 1);
}
static void denied(volatile unsigned char *p) {
    expected = 1; caught = 0;
    if (!sigsetjmp(recovery, 1)) *p = 1;
    expected = 0; CHECK(caught == SIGSEGV);
    guards++;
}
static unsigned char value(size_t i) { return (unsigned char)(i % 251 + 1); }
int main(int argc, char **argv) {
    (void)argv; CHECK(argc == 1);
    const size_t page = (size_t)sysconf(_SC_PAGESIZE), pages = 8193;
    CHECK(page == 4096);
    const size_t bytes = pages * page, align = 512 * page, total = bytes + 2 * align;
    unsigned char *raw = mmap(NULL, total, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(raw != MAP_FAILED);
    volatile unsigned char *p = (void *)(((uintptr_t)raw + 2 * align - 1) & ~(uintptr_t)(align - 1));
    CHECK(!mprotect((void *)p, bytes, PROT_READ | PROT_WRITE));
#ifdef MADV_NOHUGEPAGE
    if (madvise((void *)p, bytes, MADV_NOHUGEPAGE)) CHECK(errno == EINVAL);
#endif
    struct sigaction sa = {.sa_handler = handler}; sigemptyset(&sa.sa_mask);
    CHECK(!sigaction(SIGSEGV, &sa, NULL));
    const size_t hole = 4096;
    CHECK(!mprotect((void *)(p + hole * page), page, PROT_NONE));
    for (size_t i = 0; i < pages; i++) if (i != hole) p[i * page] = value(i);
    for (size_t i = 0; i < pages; i++) if (i != hole) {
        CHECK(p[i * page] == value(i));
        for (size_t j = 1; j < page; j++) CHECK(p[i * page + j] == 0);
    }
    denied(p - page); denied(p + hole * page); denied(p + bytes);
    CHECK(!mprotect((void *)(p + hole * page), page, PROT_READ | PROT_WRITE));
    CHECK(p[hole * page] == 0); p[hole * page] = value(hole);
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        for (size_t i = 0; i < pages; i++) p[i * page] = 0xa5;
        CHECK(!madvise((void *)(p + 1500 * page), 5000 * page, MADV_DONTNEED));
        for (size_t i = 0; i < pages; i++)
            CHECK(p[i * page] == (i >= 1500 && i < 6500 ? 0 : 0xa5));
        _exit(0);
    }
    int status; pid_t ret;
    do { ret = waitpid(child, &status, 0); } while (ret < 0 && errno == EINTR);
    CHECK(ret == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    for (size_t i = 0; i < pages; i++) CHECK(p[i * page] == value(i));
    for (size_t i = 0; i < bytes; i++) p[i] = 0x6d;
    CHECK(!madvise((void *)p, bytes, MADV_DONTNEED));
    for (size_t i = 0; i < pages; i++) p[i * page] = value(i);
    for (size_t i = 0; i < pages; i++) {
        CHECK(p[i * page] == value(i));
        for (size_t j = 1; j < page; j++) CHECK(p[i * page + j] == 0);
    }
    CHECK(!madvise((void *)p, bytes, MADV_DONTNEED));
    for (size_t i = 0; i < pages; i += 32) p[i * page] = value(i);
    unsigned char *vec = calloc(pages, 1); CHECK(vec != NULL);
    CHECK(!mincore((void *)p, bytes, (void *)vec));
    size_t resident = 0;
    for (size_t i = 0; i < pages; i++) resident += !!(vec[i] & 1);
    CHECK(resident == (pages + 31) / 32);
    for (size_t i = 0; i < pages; i += 32) {
        CHECK(p[i * page] == value(i)); CHECK(p[i * page + page - 1] == 0);
    }
    free(vec); CHECK(!munmap(raw, total));
    int rlimit_checked = 0;
#ifdef __linux__
    FILE *f = fopen("/proc/self/statm", "r"); CHECK(f != NULL);
    unsigned long virtual_pages; CHECK(fscanf(f, "%lu", &virtual_pages) == 1); CHECK(!fclose(f));
    struct rlimit old, limit; CHECK(!getrlimit(RLIMIT_AS, &old)); limit = old;
    limit.rlim_cur = (rlim_t)virtual_pages * page + 8 * 1024 * 1024;
    CHECK(limit.rlim_cur <= old.rlim_max); CHECK(!setrlimit(RLIMIT_AS, &limit));
    void *large = mmap(NULL, 16 * 1024 * 1024, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(large == MAP_FAILED && errno == ENOMEM);
    p = mmap(NULL, 4 * 1024 * 1024, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p != MAP_FAILED);
    for (size_t i = 0; i < 4 * 1024 * 1024; i += page) p[i] = 0x19;
    for (size_t i = 0; i < 4 * 1024 * 1024; i += page) CHECK(p[i] == 0x19);
    CHECK(!munmap((void *)p, 4 * 1024 * 1024)); CHECK(!setrlimit(RLIMIT_AS, &old));
    rlimit_checked = 1;
#endif
    printf("{\"schema\":\"hd.fault-window/1\",\"verified\":true,\"pages\":%zu,\"sparse_resident_pages\":%zu,\"guard_signals\":%u,\"cow\":true,\"discard\":true,\"recycled_zero\":true,\"rlimit\":%s}\n", pages, resident, guards, rlimit_checked ? "true" : "false");
    return 0;
}
