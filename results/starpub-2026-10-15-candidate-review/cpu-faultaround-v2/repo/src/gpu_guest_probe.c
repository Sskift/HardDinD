/* Validate the UML control transport against the real outer NVIDIA driver.
 * NVIDIA 550.120 nv-ioctl.h: version query has two u32s and a 64-byte string.
 * This is not a CUDA compute test or final GPU acceptance. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

struct version { uint32_t command, reply; char text[64]; };
#define VERSION_IOCTL _IOWR('F', 210, struct version)
static const char *expected;
static int failed;

static void check(const char *name, int ok) {
    printf("GPU_CONTROL_CHECK %s %s\n", name, ok ? "pass" : "fail");
    if (!ok) failed = 1;
}

static int query(int fd, struct version *v) {
    memset(v, 0, sizeof(*v)); v->command = '2';
    int result = ioctl(fd, VERSION_IOCTL, v);
    if (result || !memchr(v->text, 0, sizeof(v->text)) || strcmp(v->text, expected)) {
        fprintf(stderr, "version result=%d errno=%d reply=%u text=%.64s\n",
                result, errno, v->reply, v->text);
        return 0;
    }
    return 1;
}

struct worker { int fd, ok; };
static void *queries(void *arg) {
    struct worker *w = arg;
    struct version v;
    w->ok = 1;
    for (int i = 0; i < 32; i++) if (!query(w->fd, &v)) { w->ok = 0; break; }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: gpu-guest-probe DEVICE DRIVER_VERSION\n"); return 2; }
    setvbuf(stdout, NULL, _IOLBF, 0); expected = argv[2];
    int fd = open(argv[1], O_RDWR | O_CLOEXEC);
    check("open", fd >= 0);
    if (fd < 0) { perror("open"); return 1; }
    struct version v;
    check("real_driver_version", query(fd, &v));
    printf("GPU_CONTROL_VERSION %.64s\n", v.text);
    long page = sysconf(_SC_PAGESIZE);
    void *pages = mmap(NULL, page * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pages == MAP_FAILED) return 2;
    check("cross_page_user_pointer", query(fd, (struct version *)((char *)pages + page - 16)));
    if (mprotect(pages, page * 2, PROT_NONE)) return 2;
    errno = 0; int rc = ioctl(fd, VERSION_IOCTL, pages);
    check("invalid_user_pointer", rc == -1 && errno == EFAULT);
    if (munmap(pages, page * 2)) return 2;
    uint64_t wrong_size = 0;
    errno = 0; rc = ioctl(fd, _IOWR('F', 210, uint64_t), &wrong_size);
    int invalid_errno = errno;
    printf("GPU_CONTROL_BAD_SIZE result=%d errno=%d\n", rc, invalid_errno);
    check("driver_rejects_bad_size", rc == -1 && invalid_errno == EINVAL);
    check("success_after_driver_error", query(fd, &v));
    int duplicate = dup(fd);
    check("dup", duplicate >= 0);
    if (duplicate < 0) return 2;
    check("close_original", close(fd) == 0);
    check("query_after_dup_close", query(duplicate, &v));
    pthread_t threads[4]; struct worker workers[4]; int ok = 1;
    for (int i = 0; i < 4; i++) {
        workers[i] = (struct worker){ .fd = duplicate };
        if (pthread_create(&threads[i], NULL, queries, &workers[i])) return 2;
    }
    for (int i = 0; i < 4; i++) {
        if (pthread_join(threads[i], NULL) || !workers[i].ok) ok = 0;
    }
    check("four_threads_same_client", ok);
    check("close_last_reference", close(duplicate) == 0);
    for (int i = 0; i < 16; i++) {
        fd = open(argv[1], O_RDWR | O_CLOEXEC);
        if (fd < 0 || !query(fd, &v) || close(fd)) { ok = 0; break; }
    }
    check("reopen_lifecycle", ok);
    /* New processes open their own descriptors and independent guest mm. */
    ok = 1;
    for (int i = 0; i < 4; i++) {
        pid_t child = fork();
        if (child < 0) return 2;
        if (!child) {
            fd = open(argv[1], O_RDWR | O_CLOEXEC);
            int success = fd >= 0 && query(fd, &v);
            if (fd >= 0 && close(fd)) success = 0;
            _exit(success ? 0 : 1);
        }
        int status;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) ok = 0;
    }
    check("independent_process_lifecycle", ok);
    printf("GPU_CONTROL_END status=%d internal_cuda_verified=0\n", failed);
    return failed;
}
