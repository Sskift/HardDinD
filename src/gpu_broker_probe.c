/* Native feasibility probe for UML's shared-mm GPU broker, not a GPU backend.
 * Only preload into the private CUDA probe. The helper shares the application's
 * mm and fd table but has a distinct PID. It must never call libc: clone does
 * not give it independent libc TLS. All helper operations use raw syscalls. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#if !defined(__x86_64__)
#error This prototype is specific to the measured x86_64 Starpub host.
#endif

static long raw6(long nr, long a, long b, long c, long d, long e, long f) {
    register long r10 __asm__("r10") = d;
    register long r8 __asm__("r8") = e;
    register long r9 __asm__("r9") = f;
    long result;
    __asm__ volatile("syscall" : "=a"(result)
        : "a"(nr), "D"(a), "S"(b), "d"(c), "r"(r10), "r"(r8), "r"(r9)
        : "rcx", "r11", "memory");
    return result;
}

struct request {
    atomic_int state; /* 0 idle, 1 request, 2 response, 3 exit */
    long nr, args[6], result;
    pid_t pid;
    atomic_int tid_alive;
    void *stack;
};
static struct request requests[4];
static unsigned broker_count = 1, next_broker;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pid_t owner;
static const size_t stack_size = 1024 * 1024;
static int enabled, forward_mmap, alias_maps, failed, thread_workers;
static struct { dev_t device; int kind; } devices[40];
static unsigned device_count;
static unsigned long calls[3], maps[3], alias_ok[3], alias_failed[3];
static unsigned long alias_errno[3][256];

static int worker(void *argument) {
    struct request *request = argument;
    /* No libc TLS belongs to this raw worker, including signal handlers. */
    unsigned long blocked_signals = ~0UL;
    raw6(SYS_rt_sigprocmask, SIG_SETMASK, (long)&blocked_signals, 0, sizeof(blocked_signals), 0, 0);
    if (!thread_workers) {
        raw6(SYS_prctl, PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0, 0);
        if (raw6(SYS_getppid, 0, 0, 0, 0, 0, 0) != owner)
            raw6(SYS_exit, 122, 0, 0, 0, 0, 0);
    }
    for (;;) {
        int state = atomic_load_explicit(&request->state, memory_order_acquire);
        if (state == 3) raw6(SYS_exit, 0, 0, 0, 0, 0, 0);
        if (state != 1) {
            raw6(SYS_futex, (long)&request->state, FUTEX_WAIT, state, 0, 0, 0);
            continue;
        }
        request->result = raw6(request->nr, request->args[0], request->args[1],
                              request->args[2], request->args[3], request->args[4], request->args[5]);
        atomic_store_explicit(&request->state, 2, memory_order_release);
        raw6(SYS_futex, (long)&request->state, FUTEX_WAKE, 1, 0, 0, 0);
    }
    return 0;
}

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

/* Caller holds mutex. A dead or stuck helper makes the experiment fail. */
static long invoke(long nr, long a, long b, long c, long d, long e, long f) {
    struct request *request = &requests[next_broker++ % broker_count];
    if (failed || request->pid < 2 || getpid() != owner) return -EIO;
    request->nr = nr;
    request->args[0] = a; request->args[1] = b; request->args[2] = c;
    request->args[3] = d; request->args[4] = e; request->args[5] = f;
    atomic_store_explicit(&request->state, 1, memory_order_release);
    raw6(SYS_futex, (long)&request->state, FUTEX_WAKE, 1, 0, 0, 0);
    double deadline = now() + 30;
    while (atomic_load_explicit(&request->state, memory_order_acquire) != 2) {
        struct timespec pause = { .tv_nsec = 100000000 };
        raw6(SYS_futex, (long)&request->state, FUTEX_WAIT, 1, (long)&pause, 0, 0);
        int status, dead;
        if (thread_workers) {
            dead = !atomic_load_explicit(&request->tid_alive, memory_order_acquire);
        } else {
            pid_t reaped = waitpid(request->pid, &status, WNOHANG);
            dead = reaped == request->pid || (reaped < 0 && errno == ECHILD);
        }
        if (dead) {
            request->pid = -1; failed = 1; return -EIO;
        }
        if (now() > deadline) {
            /* A fatal signal to one CLONE_THREAD worker kills the entire
             * process. Exit as a failure instead of pretending to recover. */
            if (thread_workers) _exit(124);
            failed = 1; kill(request->pid, SIGKILL);
            while (waitpid(request->pid, NULL, 0) < 0 && errno == EINTR) {}
            request->pid = -1; return -ETIMEDOUT;
        }
    }
    long result = request->result;
    atomic_store_explicit(&request->state, 0, memory_order_release);
    return result;
}

static int device_kind(int fd) {
    struct stat st;
    int saved = errno, kind = -1;
    if (!fstat(fd, &st) && S_ISCHR(st.st_mode))
        for (unsigned i = 0; i < device_count; i++)
            if (devices[i].device == st.st_rdev) { kind = devices[i].kind; break; }
    errno = saved;
    return kind;
}

static void add_device(const char *path, int kind) {
    struct stat st;
    if (!stat(path, &st) && S_ISCHR(st.st_mode) && device_count < 40) {
        devices[device_count].device = st.st_rdev;
        devices[device_count++].kind = kind;
    }
}

__attribute__((constructor)) static void initialize(void) {
    const char *mode = getenv("HARDDIND_GPU_BROKER_MODE");
    if (!mode) return;
    if (strcmp(mode, "ioctl") && strcmp(mode, "mmap") && strcmp(mode, "alias")) {
        fprintf(stderr, "invalid GPU broker mode\n"); _exit(120);
    }
    owner = getpid();
    thread_workers = getenv("HARDDIND_GPU_BROKER_THREADS") != NULL;
    forward_mmap = strcmp(mode, "ioctl") != 0;
    alias_maps = !strcmp(mode, "alias");
    add_device("/dev/nvidiactl", 0); add_device("/dev/nvidia-uvm", 2);
    for (unsigned i = 0; i < 32; i++) {
        char path[64]; snprintf(path, sizeof(path), "/dev/nvidia%u", i);
        add_device(path, 1);
    }
    const char *count = getenv("HARDDIND_GPU_BROKER_COUNT");
    if (count) {
        if (strlen(count) != 1 || count[0] < '1' || count[0] > '4') {
            fprintf(stderr, "invalid GPU broker count\n"); _exit(120);
        }
        broker_count = count[0] - '0';
    }
    enabled = 1;
    for (unsigned i = 0; i < broker_count; i++) {
        struct request *request = &requests[i];
        request->stack = (void *)raw6(SYS_mmap, 0, stack_size, PROT_READ | PROT_WRITE,
                                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if ((long)request->stack < 0 && (long)request->stack >= -4095) {
            request->stack = NULL; failed = 1; break;
        }
        if (thread_workers)
            request->pid = clone(worker, (char *)request->stack + stack_size,
                CLONE_VM | CLONE_FILES | CLONE_SIGHAND | CLONE_THREAD |
                CLONE_PARENT_SETTID | CLONE_CHILD_CLEARTID, request,
                &request->tid_alive, NULL, &request->tid_alive);
        else
            request->pid = clone(worker, (char *)request->stack + stack_size,
                                 CLONE_VM | CLONE_FILES | SIGCHLD, request);
        if (request->pid < 0) { failed = 1; break; }
        fprintf(stderr, "HARDDIND_GPU_BROKER_START owner=%d broker=%d mode=%s slot=%u threads=%d\n",
                owner, request->pid, mode, i, thread_workers);
    }
}

int ioctl(int fd, unsigned long command, ...) {
    va_list args; va_start(args, command); void *argument = va_arg(args, void *); va_end(args);
    int kind = enabled ? device_kind(fd) : -1;
    if (kind < 0) return syscall(SYS_ioctl, fd, command, argument);
    if (getpid() != owner) { errno = EOPNOTSUPP; return -1; }
    pthread_mutex_lock(&mutex);
    calls[kind]++;
    long result = invoke(SYS_ioctl, fd, command, (long)argument, 0, 0, 0);
    pthread_mutex_unlock(&mutex);
    if (result < 0 && result >= -4095) { errno = -result; return -1; }
    return result;
}

static void *map_device(void *address, size_t length, int prot, int flags, int fd, off64_t offset) {
    int kind = enabled && forward_mmap ? device_kind(fd) : -1;
    if (kind < 0) return (void *)syscall(SYS_mmap, address, length, prot, flags, fd, offset);
    if (getpid() != owner) { errno = EOPNOTSUPP; return MAP_FAILED; }
    pthread_mutex_lock(&mutex);
    maps[kind]++;
    long result = invoke(SYS_mmap, (long)address, length, prot, flags, fd, offset);
    if (alias_maps && result >= 0) {
        /* Do not read or write MMIO. Test whether the same owned device mapping
         * can be mapped twice; UML kernel access would need a second alias. */
        long alias = invoke(SYS_mmap, 0, length, prot,
                            flags & ~(MAP_FIXED | MAP_FIXED_NOREPLACE), fd, offset);
        if (alias < 0 && alias >= -4095) {
            alias_failed[kind]++;
            if (-alias < 256) alias_errno[kind][-alias]++;
        } else {
            alias_ok[kind]++;
            if (invoke(SYS_munmap, alias, length, 0, 0, 0, 0)) failed = 1;
        }
    }
    pthread_mutex_unlock(&mutex);
    if (result < 0 && result >= -4095) { errno = -result; return MAP_FAILED; }
    return (void *)result;
}

void *mmap(void *address, size_t length, int prot, int flags, int fd, off_t offset) {
    return map_device(address, length, prot, flags, fd, offset);
}
void *mmap64(void *address, size_t length, int prot, int flags, int fd, off64_t offset) {
    return map_device(address, length, prot, flags, fd, offset);
}

__attribute__((destructor)) static void finish(void) {
    if (!enabled || getpid() != owner) return;
    pthread_mutex_lock(&mutex);
    for (unsigned i = 0; i < broker_count; i++) {
        struct request *request = &requests[i];
        if (request->pid > 1) {
            atomic_store_explicit(&request->state, 3, memory_order_release);
            raw6(SYS_futex, (long)&request->state, FUTEX_WAKE, 1, 0, 0, 0);
            if (thread_workers) {
                double deadline = now() + 30;
                int tid;
                while ((tid = atomic_load_explicit(&request->tid_alive, memory_order_acquire))) {
                    struct timespec pause = { .tv_nsec = 100000000 };
                    raw6(SYS_futex, (long)&request->tid_alive, FUTEX_WAIT, tid, (long)&pause, 0, 0);
                    if (now() > deadline) _exit(124);
                }
            } else {
                int status = 0; pid_t result;
                do { result = waitpid(request->pid, &status, 0); } while (result < 0 && errno == EINTR);
                if (result != request->pid || !WIFEXITED(status) || WEXITSTATUS(status)) failed = 1;
            }
        }
        if (request->stack) raw6(SYS_munmap, (long)request->stack, stack_size, 0, 0, 0, 0);
    }
    for (int k = 0; k < 3; k++) {
        fprintf(stderr, "HARDDIND_GPU_BROKER kind=%d ioctls=%lu mmaps=%lu alias_ok=%lu alias_failed=%lu\n",
                k, calls[k], maps[k], alias_ok[k], alias_failed[k]);
        for (int e = 1; e < 256; e++) if (alias_errno[k][e])
            fprintf(stderr, "HARDDIND_GPU_ALIAS_ERROR kind=%d errno=%d count=%lu\n", k, e, alias_errno[k][e]);
    }
    fprintf(stderr, "HARDDIND_GPU_BROKER_END failed=%d\n", failed);
    pthread_mutex_unlock(&mutex);
}
