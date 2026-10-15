/* Deterministic residency observations, plus discard, fork/COW and unmap checks. */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "fault_residency:%d: %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
static size_t page;

static unsigned resident(volatile unsigned char *p) {
    unsigned char vec[64];
    unsigned count = 0;
    CHECK(!mincore((void *)p, 64 * page, (void *)vec));
    for (unsigned i = 0; i < 64; i++) count += !!(vec[i] & 1);
    return count;
}

static void discard(volatile unsigned char *p) {
    CHECK(!madvise((void *)p, 64 * page, MADV_DONTNEED));
    CHECK(resident(p) == 0);
}

int main(void) {
    page = (size_t)sysconf(_SC_PAGESIZE);
    CHECK(page && page <= 65536);
    size_t window = page * 32, size = window * 4;
    void *raw = mmap(NULL, size, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(raw != MAP_FAILED);
    uintptr_t aligned = ((uintptr_t)raw + window - 1) & ~(uintptr_t)(window - 1);
    volatile unsigned char *p = (void *)(aligned + window);
    CHECK(!mprotect((void *)p, 64 * page, PROT_READ | PROT_WRITE));
#ifdef MADV_NOHUGEPAGE
    if (madvise((void *)p, 64 * page, MADV_NOHUGEPAGE)) CHECK(errno == EINVAL);
#endif
    CHECK(resident(p) == 0);
    p[0] = 0x11;
    p[32 * page] = 0x22;
    unsigned sparse = resident(p);
    for (unsigned i = 0; i < 64; i++)
        CHECK(p[i * page] == (i == 0 ? 0x11 : i == 32 ? 0x22 : 0));

    discard(p);
    p[0] = 0x33;
    unsigned first = resident(p);
    p[page] = 0x44;
    unsigned second = resident(p);
    for (unsigned i = 0; i < 64; i++)
        CHECK(p[i * page] == (i == 0 ? 0x33 : i == 1 ? 0x44 : 0));

    discard(p);
    int fd[2];
    CHECK(!pipe(fd));
    p[0] = 0x55;
    /* Raw clone avoids libc child bookkeeping faults before the test access. */
#ifdef __linux__
    pid_t child = (pid_t)syscall(SYS_clone, SIGCHLD, NULL, NULL, NULL, 0);
#else
    pid_t child = fork();
#endif
    CHECK(child >= 0);
    if (!child) {
        p[page] = 0x77;
        unsigned count = resident(p);
        p[0] = 0x88;
        CHECK(write(fd[1], &count, sizeof(count)) == (ssize_t)sizeof(count));
        _exit(0);
    }
    CHECK(!close(fd[1]));
    unsigned fork_pages;
    ssize_t got;
    do { got = read(fd[0], &fork_pages, sizeof(fork_pages)); } while (got < 0 && errno == EINTR);
    CHECK(got == (ssize_t)sizeof(fork_pages));
    CHECK(!close(fd[0]));
    int status;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    CHECK(waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    CHECK(p[0] == 0x55 && p[page] == 0);
    CHECK(!munmap(raw, size));
    unsigned char vec[64];
    CHECK(mincore((void *)p, 64 * page, (void *)vec) == -1 && errno == ENOMEM);
    printf("{\"schema\":\"hd.fault-residency/1\",\"verified\":true,\"page_bytes\":%zu,"
           "\"sparse_pages\":%u,\"dense_first_pages\":%u,\"dense_second_pages\":%u,"
           "\"fork_child_pages\":%u}\n", page, sparse, first, second, fork_pages);
    return 0;
}
