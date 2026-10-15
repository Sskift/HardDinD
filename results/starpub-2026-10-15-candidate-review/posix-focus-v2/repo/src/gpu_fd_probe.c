/* NVIDIA 550.120 REGISTER_FD semantics; this is not CUDA acceptance. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#define REGISTER_FD _IOWR('F', 201, int)
static const char *prefix;
static int failed;
static void check(const char *name, int ok) {
    printf("GPU_FD_CHECK %s %s\n", name, ok ? "pass" : "fail");
    if (!ok) failed = 1;
}
static int device(int index) {
    char path[256];
    if (index < 0) snprintf(path, sizeof(path), "%sctl", prefix);
    else snprintf(path, sizeof(path), "%s%d", prefix, index);
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) perror(path);
    return fd;
}
static int registration(int target, int source, int expected_errno) {
    int arg = source;
    errno = 0;
    int rc = ioctl(target, REGISTER_FD, &arg), error = errno;
    int ok = rc == (expected_errno ? -1 : 0) && (!expected_errno || error == expected_errno) && arg == source;
    if (!ok) fprintf(stderr, "register target=%d source=%d rc=%d errno=%d expected=%d arg=%d\n",
                     target, source, rc, error, expected_errno, arg);
    return ok;
}
struct client { int ctl, ok; };
static void *thread_client(void *arg) {
    struct client *client = arg;
    int fd = device(0);
    client->ok = fd >= 0 && registration(fd, client->ctl, 0) && !close(fd);
    return NULL;
}
static void usercopy(void) {
    long size = sysconf(_SC_PAGESIZE);
    char *page = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    int pipes[2]; char data[4] = {0};
    if (page == MAP_FAILED || pipe2(pipes, O_NONBLOCK | O_CLOEXEC)) exit(2);
    memset(page, 0x5a, 4);
    if (mprotect(page, size, PROT_NONE)) exit(2);
    errno = 0; ssize_t n = write(pipes[1], page, 4); int error = errno;
    check("pipe_read_from_protnone", n == -1 && error == EFAULT);
    if (n > 0) (void)!read(pipes[0], data, sizeof(data));
    if (mprotect(page, size, PROT_READ)) exit(2);
    int ok = write(pipes[1], page, 4) == 4;
    errno = 0; n = read(pipes[0], page, 4); error = errno;
    check("pipe_write_to_readonly", ok && n == -1 && error == EFAULT);
    check("pipe_recovery_preserves_data", read(pipes[0], data, 4) == 4 &&
          !memcmp(data, "ZZZZ", 4));
    close(pipes[0]); close(pipes[1]); munmap(page, size);
}
int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: gpu-fd-probe DEVICE_PREFIX GPU_COUNT\n"); return 2; }
    prefix = argv[1]; int count = atoi(argv[2]);
    if (count < 1 || count > 255) return 2;
    setvbuf(stdout, NULL, _IOLBF, 0);
    usercopy();
    int ctl = device(-1), target = device(0);
    check("open_control_and_gpu", ctl >= 0 && target >= 0);
    if (ctl < 0 || target < 0) return 1;
    check("invalid_source_fd", registration(target, -1, EINVAL));
    int other = open("/dev/null", O_RDWR | O_CLOEXEC);
    check("non_device_source_fd", other >= 0 && registration(target, other, EINVAL));
    if (other >= 0) close(other);
    check("gpu_source_rejected", registration(target, target, EINVAL));
    check("control_self_reference", registration(ctl, ctl, EINVAL));
    int high = fcntl(ctl, F_DUPFD_CLOEXEC, 200);
    check("high_guest_fd", high >= 200 && !close(ctl));
    if (high < 200) return 2;
    check("high_fd_registration", registration(target, high, 0));
    check("duplicate_registration", registration(target, high, EINVAL));
    check("source_close", !close(high));
    /* Driver reference survives source close; the target remains registered. */
    ctl = device(-1);
    check("target_retains_registration", ctl >= 0 && registration(target, ctl, EINVAL));
    check("close_registered_target", !close(target));
    int ok = 1;
    for (int index = 0; index < count; index++) {
        target = device(index);
        if (target < 0 || !registration(target, ctl, 0) || close(target)) ok = 0;
    }
    check("all_gpu_devices_registration", ok);
    target = device(-1);
    int third = device(-1);
    check("control_target_registration", target >= 0 && registration(target, ctl, 0));
    check("linked_control_as_source_rejected", third >= 0 && registration(third, target, EINVAL));
    if (third >= 0) close(third);
    if (target >= 0) close(target);
    long page_size = sysconf(_SC_PAGESIZE);
    int *page = mmap(NULL, page_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) return 2;
    *page = ctl;
    target = device(0);
    if (target < 0 || mprotect(page, page_size, PROT_NONE)) return 2;
    errno = 0; int rc = ioctl(target, REGISTER_FD, page), error = errno;
    check("copyin_fault", rc == -1 && error == EFAULT);
    check("copyin_fault_does_not_register", registration(target, ctl, 0));
    close(target);
    if (mprotect(page, page_size, PROT_READ)) return 2;
    target = device(0);
    errno = 0; rc = ioctl(target, REGISTER_FD, page); error = errno;
    check("copyout_fault", rc == -1 && error == EFAULT);
    check("copyout_fault_keeps_registration", registration(target, ctl, EINVAL));
    close(target); munmap(page, page_size);
    pthread_t threads[4]; struct client clients[4]; ok = 1;
    for (int i = 0; i < 4; i++) {
        clients[i] = (struct client){ .ctl = ctl };
        if (pthread_create(&threads[i], NULL, thread_client, &clients[i])) return 2;
    }
    for (int i = 0; i < 4; i++) if (pthread_join(threads[i], NULL) || !clients[i].ok) ok = 0;
    check("four_threads_shared_control", ok);
    close(ctl); ok = 1;
    for (int i = 0; i < 16; i++) {
        ctl = device(-1); target = device(i % count);
        if (ctl < 0 || target < 0 || !registration(target, ctl, 0)) ok = 0;
        if (ctl >= 0 && close(ctl)) ok = 0;
        if (target >= 0 && close(target)) ok = 0;
    }
    check("reopen_lifecycle", ok);
    ok = 1;
    for (int i = 0; i < 4; i++) {
        pid_t child = fork(); if (child < 0) return 2;
        if (!child) {
            ctl = device(-1); target = device(i % count);
            int success = ctl >= 0 && target >= 0 && registration(target, ctl, 0);
            if (ctl >= 0 && close(ctl)) success = 0;
            if (target >= 0 && close(target)) success = 0;
            _exit(success ? 0 : 1);
        }
        int status;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) ok = 0;
    }
    check("independent_process_lifecycle", ok);
    printf("GPU_FD_END status=%d internal_cuda_verified=0\n", failed);
    return failed;
}
