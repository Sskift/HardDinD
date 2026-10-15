#include "common.h"
#include <dirent.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>

#define MIB (1024UL * 1024)
static double duration;
static int quick;
static volatile uint64_t sink;

static void child_ok(pid_t pid) {
    int status;
    require(pid > 0 && waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0, "child failed");
}
static void command(char *const args[]) {
    pid_t pid = fork(); require(pid >= 0, "fork command");
    if (!pid) { execv(args[0], args); perror("exec"); _exit(127); }
    child_ok(pid);
}
static void shell(const char *text) {
    char *args[] = {"/bin/sh", "-ec", (char *)text, NULL}; command(args);
}
static void *allocate(size_t size) {
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    require(p != MAP_FAILED, "mmap"); return p;
}
static void release(void *p, size_t size) { require(munmap(p, size) == 0, "munmap"); }

/* Ten scalar integer/FP operations per iteration, deterministic per batch. */
__attribute__((noinline)) static uint64_t compute(uint64_t seed) {
    uint64_t x = seed; double a = 0.5, b = 0.25;
    for (int j = 0; j < 8192; j++) {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        a = a * 0.999999 + b; b = b * 0.99999 + 0.000001;
    }
    require(isfinite(a) && a > 100 && b > 0, "compute FP");
    return x ^ (uint64_t)(a * 1000000);
}
struct cpu_task { uint64_t count, check; double elapsed; };
static pthread_barrier_t barrier;
static void *compute_thread(void *arg) {
    struct cpu_task *t = arg;
    uint64_t reference = compute(123456789), count = 0, check = 0;
    pthread_barrier_wait(&barrier);
    double start = now();
    do {
        /* The volatile load prevents hoisting the deterministic batch. */
        volatile uint64_t seed = 123456789;
        uint64_t value = compute(seed); require(value == reference, "compute checksum");
        check += value; count++;
    } while (now() - start < duration);
    t->elapsed = now() - start; t->count = count; t->check = check;
    return NULL;
}
static void cpu(int threads) {
    pthread_t ids[4]; struct cpu_task tasks[4] = {{0}};
    require(pthread_barrier_init(&barrier, NULL, (unsigned)threads) == 0, "barrier");
    for (int j = 0; j < threads; j++)
        require(pthread_create(&ids[j], NULL, compute_thread, &tasks[j]) == 0, "cpu thread");
    double elapsed = 0; uint64_t count = 0, check = 0;
    for (int j = 0; j < threads; j++) {
        require(pthread_join(ids[j], NULL) == 0, "cpu join");
        if (tasks[j].elapsed > elapsed) elapsed = tasks[j].elapsed;
        count += tasks[j].count; check ^= tasks[j].check + (uint64_t)j;
    }
    pthread_barrier_destroy(&barrier);
    result(count * 8192.0 * 10 / elapsed, check ? check : count);
}
static void memory(int touch) {
    size_t size = (quick ? 64 : 512) * MIB;
    uint64_t bytes = 0, check = 0; double start, elapsed;
    if (touch) {
        start = now();
        do {
            volatile unsigned char *p = allocate(size);
            for (size_t j = 0; j < size; j += 4096) p[j] = 0x5a;
            for (size_t j = 0; j < size; j += 4096) { require(p[j] == 0x5a, "first-touch"); check++; }
            release((void *)p, size); bytes += size;
        } while (now() - start < duration);
        elapsed = now() - start;
    } else {
        unsigned char *a = allocate(size), *b = allocate(size);
        memset(a, 0xa5, size); memset(b, 0x5a, size);
        start = now();
        do {
            memcpy(b, a, size);
            __asm__ volatile("" : : "r"(b) : "memory");
            bytes += size;
            unsigned char *tmp = a; a = b; b = tmp;
        } while (now() - start < duration);
        elapsed = now() - start;
        for (size_t j = 0; j < size; j++) { require(b[j] == 0xa5, "memcpy data"); check += b[j]; }
        release(a, size); release(b, size);
    }
    result(bytes / elapsed / 1e9, check);
}
static void fill(unsigned char *p, size_t size) {
    for (size_t j = 0; j < size; j++) p[j] = (unsigned char)((j * 131 + j / 251) % 256);
}
/* Read is a warm page-cache read after write+fsync, not a cold-device test. */
static void disk(int reading) {
    size_t size = (quick ? 256 : 1024) * MIB;
    unsigned char *expected = malloc(MIB), *buffer = malloc(MIB);
    require(expected && buffer, "disk buffers"); fill(expected, MIB);
    int fd = open("data", O_CREAT | O_TRUNC | O_RDWR, 0600); require(fd >= 0, "disk open");
    double start = now();
    for (size_t offset = 0; offset < size; offset += MIB) {
        memcpy(expected, &offset, sizeof(offset)); write_all(fd, expected, MIB);
    }
    require(fsync(fd) == 0, "disk fsync");
    double elapsed = now() - start;
    require(lseek(fd, 0, SEEK_SET) == 0, "disk rewind");
    double read_start = now();
    for (size_t offset = 0; offset < size; offset += MIB) {
        memcpy(expected, &offset, sizeof(offset));
        read_all(fd, buffer, MIB);
        require(memcmp(expected, buffer, MIB) == 0, "disk readback mismatch");
    }
    if (reading) elapsed = now() - read_start;
    require(close(fd) == 0 && unlink("data") == 0, "disk cleanup");
    free(expected); free(buffer); result(size / elapsed / 1e6, size);
}
static void metadata(void) {
    int count = quick ? 2000 : 20000;
    require(mkdir("files", 0700) == 0 && chdir("files") == 0, "metadata mkdir");
    double start = now();
    for (int phase = 0; phase < 4; phase++) {
        for (int i = 0; i < count; i++) {
            char old[32], renamed[32]; struct stat st;
            snprintf(old, sizeof(old), "f%06d", i); snprintf(renamed, sizeof(renamed), "g%06d", i);
            if (phase == 0) {
                int fd = open(old, O_CREAT | O_EXCL | O_WRONLY, 0600); require(fd >= 0, "metadata create");
                write_all(fd, &i, sizeof(i)); require(close(fd) == 0, "metadata close");
            } else if (phase == 1) require(stat(old, &st) == 0 && st.st_size == (off_t)sizeof(i), "metadata stat");
            else if (phase == 2) require(rename(old, renamed) == 0, "metadata rename");
            else require(unlink(renamed) == 0, "metadata unlink");
        }
    }
    double elapsed = now() - start;
    DIR *dir = opendir("."); require(dir != NULL, "metadata opendir");
    struct dirent *entry;
    while ((entry = readdir(dir))) require(!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."), "metadata residue");
    closedir(dir); require(chdir("..") == 0 && rmdir("files") == 0, "metadata cleanup");
    result(count * 4.0 / elapsed, (uint64_t)count);
}
static void applications(const char *name) {
    double start, elapsed; uint64_t verified = 1;
    if (!strcmp(name, "app.compile")) {
        shell("rm -rf lua; mkdir lua; tar -xzf /bench/lua.tar.gz -C lua --strip-components=1");
        start = now();
        shell("cd lua; make -j4 generic > /work/compile.log 2>&1 || { cat /work/compile.log >&2; exit 1; }");
        elapsed = now() - start;
        shell("lua/src/lua -e 'local n=0; for i=1,10000 do n=n+i end; assert(n==50005000); assert(_VERSION==\"Lua 5.4\")'; lua/src/luac -p /bench/verify.lua");
        shell("rm -rf lua");
    } else if (!strcmp(name, "app.untar")) {
        require(mkdir("untar", 0700) == 0, "untar mkdir");
        start = now(); shell(quick ? "tar -xf /bench/small-quick.tar -C untar" : "tar -xf /bench/small.tar -C untar");
        int directory = open("untar", O_RDONLY | O_DIRECTORY); require(directory >= 0, "untar directory");
        /* syncfs limits the flush to this filesystem; no host-wide sync. */
        require(syncfs(directory) == 0, "untar syncfs"); elapsed = now() - start; close(directory);
        int count = 0; DIR *dir = opendir("untar"); require(dir != NULL, "untar opendir");
        struct dirent *entry;
        while ((entry = readdir(dir))) {
            if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
            char path[300]; snprintf(path, sizeof(path), "untar/%s", entry->d_name);
            int fd = open(path, O_RDONLY); require(fd >= 0, "untar verify open");
            char data[32], expected[32]; struct stat st;
            snprintf(expected, sizeof(expected), "harddind-file-%05d\n", atoi(entry->d_name));
            size_t n = strlen(expected);
            require(fstat(fd, &st) == 0 && (size_t)st.st_size == n, "untar verify size");
            read_all(fd, data, n); require(memcmp(data, expected, n) == 0, "untar verify content");
            close(fd); require(unlink(path) == 0, "untar cleanup"); count++;
        }
        closedir(dir); require(count == (quick ? 2000 : 20000) && rmdir("untar") == 0, "untar count"); verified = (uint64_t)count;
    } else {
        size_t size = (quick ? 64 : 256) * MIB;
        unsigned char *buffer = malloc(MIB); require(buffer != NULL, "gzip buffer");
        /* Repeat a nonzero, varied 4 KiB pattern; compression is not sparse zeros. */
        fill(buffer, 4096);
        for (size_t j = 4096; j < MIB; j += 4096) memcpy(buffer + j, buffer, 4096);
        int fd = open("gzip.in", O_CREAT | O_TRUNC | O_WRONLY, 0600); require(fd >= 0, "gzip open");
        for (size_t j = 0; j < size; j += MIB) write_all(fd, buffer, MIB);
        close(fd); free(buffer);
        start = now(); shell("gzip -6 -c gzip.in > gzip.out"); elapsed = now() - start;
        shell("gzip -dc gzip.out > gzip.check; cmp gzip.in gzip.check; rm gzip.in gzip.out gzip.check"); verified = size;
    }
    result(elapsed, verified);
}
static volatile sig_atomic_t delivered;
static void handler(int signo) { if (signo == SIGUSR1) delivered++; }
static void *echo_pointer(void *p) { return p; }
static void micro(const char *name) {
    uint64_t count = 0; double start;
    int a[2] = {-1,-1}, b[2] = {-1,-1}; pid_t peer = -1;
    if (!strcmp(name, "micro.pipe")) {
        require(pipe(a) == 0 && pipe(b) == 0, "pipe");
        peer = fork(); require(peer >= 0, "pipe fork");
        if (!peer) {
            close(a[1]); close(b[0]); uint64_t word; ssize_t n;
            while ((n = read(a[0], &word, sizeof(word))) != 0) {
                if (n < 0 && errno == EINTR) continue;
                require(n == (ssize_t)sizeof(word), "pipe request"); write_all(b[1], &word, sizeof(word));
            }
            close(a[0]); close(b[1]); _exit(0);
        }
        close(a[0]); close(b[1]);
    }
    if (!strcmp(name, "micro.signal")) {
        struct sigaction action = {.sa_handler = handler}; sigemptyset(&action.sa_mask);
        require(sigaction(SIGUSR1, &action, NULL) == 0, "sigaction");
    }
    pid_t self = getpid(); struct timespec ts = {0};
    int batch = (!strcmp(name, "micro.syscall") || !strcmp(name, "micro.clock")) ? 4096 : 1;
    start = now();
    do {
        for (int j = 0; j < batch; j++) {
            if (!strcmp(name, "micro.syscall")) require(syscall(SYS_getpid) == self, "getpid");
            else if (!strcmp(name, "micro.clock")) {
                struct timespec next; require(clock_gettime(CLOCK_MONOTONIC, &next) == 0, "clock");
                require(next.tv_sec > ts.tv_sec || (next.tv_sec == ts.tv_sec && next.tv_nsec >= ts.tv_nsec), "clock reversed"); ts = next;
            } else if (!strcmp(name, "micro.forkexec")) {
                char *args[] = {"/bench/true", NULL}; command(args);
            } else if (!strcmp(name, "micro.mmap")) {
                volatile uint64_t *p = allocate(4096); *p = count + 1;
                require(*p == count + 1, "mmap touch"); sink = *p; release((void *)p, 4096);
            } else if (!strcmp(name, "micro.thread")) {
                pthread_t thread; void *output = NULL;
                require(pthread_create(&thread, NULL, echo_pointer, &count) == 0, "pthread create");
                require(pthread_join(thread, &output) == 0 && output == &count, "pthread join");
            } else if (!strcmp(name, "micro.signal")) {
                delivered = 0; require(kill(self, SIGUSR1) == 0 && delivered == 1, "signal delivery");
            } else if (!strcmp(name, "micro.pipe")) {
                uint64_t reply; write_all(a[1], &count, sizeof(count)); read_all(b[0], &reply, sizeof(reply));
                require(reply == count, "pipe reply");
            } else require(0, "unknown micro");
            count++;
        }
    } while (now() - start < duration);
    double elapsed = now() - start;
    if (peer > 0) { close(a[1]); close(b[0]); child_ok(peer); }
    result(count / elapsed, count);
}
int main(int argc, char **argv) {
    require(argc == 3, "usage: workload METRIC QUICK"); quick = atoi(argv[2]); duration = quick ? 0.15 : 1.0;
    require(chdir("/work") == 0, "work directory");
    const char *name = argv[1];
    if (!strcmp(name, "cpu.single")) cpu(1);
    else if (!strcmp(name, "cpu.multi")) cpu(4);
    else if (!strcmp(name, "mem.bandwidth")) memory(0);
    else if (!strcmp(name, "mem.touch")) memory(1);
    else if (!strcmp(name, "disk.seq_write") || !strcmp(name, "disk.volume_write")) disk(0);
    else if (!strcmp(name, "disk.seq_read")) disk(1);
    else if (!strcmp(name, "fs.metadata")) metadata();
    else if (!strncmp(name, "app.", 4)) applications(name);
    else if (!strncmp(name, "micro.", 6)) micro(name);
    else require(0, "unknown metric");
    return 0;
}
