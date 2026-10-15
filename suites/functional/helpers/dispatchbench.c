/* The same static binary exercises real syscall families on host and guest. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static const uintptr_t probe_address = 0x510000000000ULL;
static const char *program;
static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(1);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
static void check(int ok, const char *what) {
    if (!ok) { perror(what); exit(1); }
}
static void *thread_syscalls(void *arg) {
    long pid = *(long *)arg, tid = syscall(SYS_gettid);
    check(tid > 0 && tid != pid, "thread identity");
    for (int i = 0; i < 10000; i++) {
        check(syscall(SYS_getpid) == pid, "shared pid");
        check(syscall(SYS_gettid) == tid, "stable tid");
    }
    return (void *)(intptr_t)tid;
}
static void *parallel_exec(void *arg) {
    (void)arg;
    for (int i = 0; i < 24; i++) {
        pid_t pid = fork();
        check(pid >= 0, "parallel fork");
        if (!pid) {
            execl(program, program, "isolation-reader", NULL);
            _exit(127);
        }
        int status;
        pid_t waited;
        do { waited = waitpid(pid, &status, 0); } while (waited < 0 && errno == EINTR);
        check(waited == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0, "parallel child");
    }
    return NULL;
}
static int isolation_child(int writer) {
    void *address = (void *)probe_address;
    if (writer) {
        void *p = mmap(address, 4096, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
        check(p == address, "isolated map");
        memset(p, 0xa5, 4096);
        /* Leave the mapping present until exit. */
    } else {
        unsigned char resident;
        errno = 0;
        check(mincore(address, 4096, &resident) == -1 && errno == ENOMEM,
              "previous address space must be absent");
    }
    return 0;
}
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    program = argv[0];
    if (!strcmp(argv[1], "isolation-writer")) return isolation_child(1);
    if (!strcmp(argv[1], "isolation-reader")) return isolation_child(0);
    double start = now();
    int workers = 1;
    if (!strcmp(argv[1], "identity")) {
        long pid = getpid(), ppid = getppid(), tid = syscall(SYS_gettid);
        long uid = getuid(), gid = getgid();
        for (int i = 0; i < 20000; i++) {
            check(syscall(SYS_getpid) == pid, "getpid");
            check(syscall(SYS_getppid) == ppid, "getppid");
            check(syscall(SYS_gettid) == tid, "gettid");
            check(syscall(SYS_getuid) == uid, "getuid");
            check(syscall(SYS_getgid) == gid, "getgid");
        }
    } else if (!strcmp(argv[1], "fdio")) {
        for (int i = 0; i < 2000; i++) {
            unsigned char buf[64];
            struct stat st;
            int in = open("/dev/zero", O_RDONLY | O_CLOEXEC);
            int out = open("/dev/null", O_WRONLY | O_CLOEXEC);
            check(in >= 0 && out >= 0, "open");
            check(fstat(in, &st) == 0 && S_ISCHR(st.st_mode), "fstat");
            memset(buf, 0xff, sizeof(buf));
            check(read(in, buf, sizeof(buf)) == sizeof(buf), "read");
            for (unsigned j = 0; j < sizeof(buf); j++) check(buf[j] == 0, "read data");
            check(write(out, buf, sizeof(buf)) == sizeof(buf), "write");
            check(close(in) == 0 && close(out) == 0, "close");
        }
    } else if (!strcmp(argv[1], "mapping")) {
        for (int i = 0; i < 512; i++) {
            size_t bytes = 65536;
            unsigned char *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            check(p != MAP_FAILED, "mmap");
            memset(p, (unsigned char)i, bytes);
            check(mprotect(p, bytes, PROT_READ) == 0, "mprotect");
            for (size_t j = 0; j < bytes; j += 4096) check(p[j] == (unsigned char)i, "mapped data");
            check(munmap(p, bytes) == 0, "munmap");
        }
    } else if (!strcmp(argv[1], "thread_syscalls")) {
        pthread_t ids[4];
        void *tids[4];
        long pid = getpid();
        workers = 4;
        for (int i = 0; i < 4; i++) check(pthread_create(&ids[i], NULL, thread_syscalls, &pid) == 0, "create");
        for (int i = 0; i < 4; i++) check(pthread_join(ids[i], &tids[i]) == 0, "join");
        for (int i = 0; i < 4; i++)
            for (int j = i + 1; j < 4; j++) check(tids[i] != tids[j], "unique tids");
    } else if (!strcmp(argv[1], "parallel_exec")) {
        pthread_t ids[4];
        workers = 4;
        for (int i = 0; i < 4; i++) check(pthread_create(&ids[i], NULL, parallel_exec, NULL) == 0, "create");
        for (int i = 0; i < 4; i++) check(pthread_join(ids[i], NULL) == 0, "join");
    } else if (!strcmp(argv[1], "isolation")) {
        for (int i = 0; i < 48; i++) {
            pid_t pid = fork();
            check(pid >= 0, "fork");
            if (!pid) {
                execl(argv[0], argv[0], i % 2 ? "isolation-reader" : "isolation-writer", NULL);
                _exit(127);
            }
            int status;
            pid_t waited;
            do { waited = waitpid(pid, &status, 0); } while (waited < 0 && errno == EINTR);
            check(waited == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0, "isolation child");
        }
    } else return 2;
    printf("{\"test\":\"%s\",\"workers\":%d,\"seconds\":%.9f,\"verified\":true}\n",
           argv[1], workers, now() - start);
    return 0;
}
