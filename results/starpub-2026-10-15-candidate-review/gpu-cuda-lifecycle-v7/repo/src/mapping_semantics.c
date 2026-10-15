/* Exercise actual VM effects, including paths that must not use mprotect. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "mapping check failed line %d: %s\n", __LINE__, #x); exit(1); } } while (0)
static sigjmp_buf recovery;
static volatile sig_atomic_t expected, caught;
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
        else { volatile unsigned char c = *p; (void)c; }
    }
    expected = 0;
    CHECK(caught == sig);
}
int main(void) {
    size_t page = (size_t)sysconf(_SC_PAGESIZE), length = 32 * page;
    struct sigaction sa = {.sa_handler = fault};
    sigemptyset(&sa.sa_mask);
    CHECK(!sigaction(SIGSEGV, &sa, NULL) && !sigaction(SIGBUS, &sa, NULL));
    unsigned char *p = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p != MAP_FAILED);
    for (int round = 0; round < 64; round++) {
        /* Cold PTEs and DONTNEED must still fault in the correct new backing. */
        CHECK(!mprotect(p, length, PROT_NONE));
        denied(p, 0, SIGSEGV);
        CHECK(!mprotect(p, length, PROT_READ | PROT_WRITE));
        for (size_t i = 0; i < length; i += page) { CHECK(p[i] == 0); p[i] = (unsigned char)(i / page + 1); }
        CHECK(!mprotect(p, length, PROT_READ));
        for (size_t i = 0; i < length; i += page) { CHECK(p[i] == i / page + 1); denied(p + i, 1, SIGSEGV); }
        CHECK(!madvise(p, length, MADV_DONTNEED));
    }
    CHECK(!munmap(p, length));
    puts("MAPPING_CHECK lazy_discard_and_protection pass");

    int first = memfd_create("mapping-first", MFD_CLOEXEC), second = memfd_create("mapping-second", MFD_CLOEXEC);
    CHECK(first >= 0 && second >= 0);
    CHECK(!ftruncate(first, length) && !ftruncate(second, length));
    unsigned char *a = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, first, 0);
    unsigned char *b = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, second, 0);
    CHECK(a != MAP_FAILED && b != MAP_FAILED);
    memset(a, 0x35, length); memset(b, 0x79, length);
    p = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_PRIVATE, first, 0);
    CHECK(p != MAP_FAILED);
    for (int round = 0; round < 64; round++) {
        int fd = round & 1 ? first : second;
        int value = round & 1 ? 0x35 : 0x79;
        CHECK(mmap(p, length, PROT_READ | PROT_WRITE, MAP_FIXED | MAP_PRIVATE, fd, 0) == p);
        CHECK(!mprotect(p, length, PROT_READ));
        for (size_t i = 0; i < length; i += page) CHECK(p[i] == value);
        CHECK(!mprotect(p, length, PROT_READ | PROT_WRITE));
        memset(p, 0xa4, length);
        for (size_t i = 0; i < length; i += page) CHECK(a[i] == 0x35 && b[i] == 0x79);
    }
    puts("MAPPING_CHECK fixed_file_replacement_private_cow pass");
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        CHECK(!mprotect(p, length, PROT_READ));
        CHECK(!mprotect(p, length, PROT_READ | PROT_WRITE));
        memset(p, 0xe2, length);
        _exit(0);
    }
    int status; CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
    for (size_t i = 0; i < length; i += page) CHECK(p[i] == 0xa4);
    CHECK(!munmap(p, length));
    CHECK(!ftruncate(first, 0));
    denied(a, 0, SIGBUS);
    CHECK(!ftruncate(first, length));
    CHECK(!mprotect(a, length, PROT_READ));
    for (size_t i = 0; i < length; i += page) CHECK(a[i] == 0);
    CHECK(!munmap(a, length) && !munmap(b, length));
    CHECK(!close(first) && !close(second));
    puts("MAPPING_CHECK fork_cow_and_file_truncate pass");
    puts("MAPPING_SEMANTICS_END status=0");
    return 0;
}
