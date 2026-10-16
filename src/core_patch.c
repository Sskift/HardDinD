/* Syscall-site rewriting: concurrent entry, signals, private text and lifetime. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core patch failure line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define SITES 32
#define CALLS 1024
#define OFFSET 32
static const unsigned char original[] = {0xb8, 39, 0, 0, 0, 0x0f, 0x05, 0xc3};
extern long core_patch_site(void);
__asm__(".text\n.global core_patch_site\n.type core_patch_site,@function\n"
        ".rept 14; nop; .endr\ncore_patch_site: mov $39,%eax; syscall; ret\n.size core_patch_site,.-core_patch_site\n");
static pthread_barrier_t gate;
static _Atomic unsigned int done, delivered;
static _Atomic int tids[4];
static unsigned char *mapping;
static size_t page_size;
static long pid;
static int enabled;
static unsigned int hot;

static void signal_handler(int sig) {
    if (sig == SIGUSR1) atomic_fetch_add_explicit(&delivered, 1, memory_order_relaxed);
}
static void barrier(void) {
    int r = pthread_barrier_wait(&gate);
    CHECK(!r || r == PTHREAD_BARRIER_SERIAL_THREAD);
}
static void *worker(void *arg) {
    unsigned int id = (unsigned int)(uintptr_t)arg;
    atomic_store(&tids[id], (int)syscall(SYS_gettid));
    for (unsigned int site = 0; site < SITES; site++) {
        long (*call)(void) = (void *)(mapping + site * page_size + OFFSET);
        barrier();
        for (unsigned int n = 0; n < CALLS; n++) CHECK(call() == pid);
        barrier();
    }
    atomic_fetch_add(&done, 1);
    return NULL;
}
static void wait_ok(pid_t child) {
    int status;
    CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}
static int is_original(const void *ptr) {
    const volatile unsigned char *p = ptr;
    for (unsigned int i = 0; i < sizeof(original); i++) if (p[i] != original[i]) return 0;
    return 1;
}
static void verify_site(const void *ptr) {
    const volatile unsigned char *p = ptr;
    if (enabled) {
        CHECK(p[0] == 0xff && p[1] == 0x24 && p[2] == 0x25 && p[5] == 0x06);
    } else CHECK(is_original(ptr));
}
static void read_enabled(void) {
    FILE *f = fopen("/proc/uml_syscall_patch", "r");
    if (!f) { CHECK(errno == ENOENT); return; }
    char key[64]; unsigned long long value;
    CHECK(fscanf(f, "%63s %llu", key, &value) == 2 && !strcmp(key, "enabled") && value <= 1);
    enabled = (int)value;
    while (fscanf(f, "%63s %llu", key, &value) == 2)
        if (!strcmp(key, "hot_threshold")) { CHECK(value <= CALLS); hot = (unsigned int)value; }
    CHECK(feof(f) && !ferror(f) && !fclose(f));
}
static void warm(long (*call)(void), long expected) {
    for (unsigned int n = 0; n < (hot ? hot : 1); n++) CHECK(call() == expected);
}
static void ptrace_after_patch(void) {
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        CHECK(!ptrace(PTRACE_TRACEME, 0, NULL, NULL));
        CHECK(!syscall(SYS_tgkill, getpid(), syscall(SYS_gettid), SIGSTOP));
        long expected = getpid();
        for (int i = 0; i < 64; i++) CHECK(core_patch_site() == expected);
        _exit(0);
    }
    int status, stops = 0;
    struct user_regs_struct regs;
    CHECK(waitpid(child, &status, 0) == child && WIFSTOPPED(status));
    CHECK(!ptrace(PTRACE_SETOPTIONS, child, NULL,
                 (void *)(uintptr_t)(PTRACE_O_TRACESYSGOOD | PTRACE_O_EXITKILL)));
    for (;;) {
        CHECK(!ptrace(PTRACE_SYSCALL, child, NULL, NULL));
        CHECK(waitpid(child, &status, 0) == child);
        if (WIFEXITED(status)) { CHECK(WEXITSTATUS(status) == 0); break; }
        CHECK(WIFSTOPPED(status) && WSTOPSIG(status) == (SIGTRAP | 0x80));
        CHECK(!ptrace(PTRACE_GETREGS, child, NULL, &regs));
        if (regs.orig_rax == SYS_getpid) stops++;
        else CHECK(regs.orig_rax == SYS_exit_group);
    }
    CHECK(stops == 130);
}
static void debugger_writes(void) {
    unsigned char *site = mapping + OFFSET;
    for (int mode = 0; mode < 3; mode++) {
        pid_t child = fork(); CHECK(child >= 0);
        if (!child) {
            verify_site(site);
            CHECK(!ptrace(PTRACE_TRACEME, 0, NULL, NULL));
            CHECK(!syscall(SYS_tgkill, getpid(), syscall(SYS_gettid), SIGSTOP));
            CHECK(((long (*)(void))site)() == (mode == 1 ? getppid() : getpid()));
            _exit(0);
        }
        int status;
        struct user_regs_struct regs, saved;
        CHECK(waitpid(child, &status, 0) == child && WIFSTOPPED(status));
        CHECK(!ptrace(PTRACE_SETOPTIONS, child, NULL, (void *)(uintptr_t)PTRACE_O_EXITKILL));
        if (mode == 1) {
            char path[64];
            CHECK(snprintf(path, sizeof(path), "/proc/%d/mem", child) > 0);
            int fd = open(path, O_WRONLY); CHECK(fd >= 0);
            uint32_t nr = SYS_getppid;
            CHECK(pwrite(fd, &nr, sizeof(nr), (off_t)(uintptr_t)(site + 1)) == sizeof(nr));
            CHECK(!close(fd));
        } else {
            errno = 0;
            long word = ptrace(PTRACE_PEEKTEXT, child, site, NULL); CHECK(!errno);
            if (mode == 0) {
                CHECK(!ptrace(PTRACE_POKETEXT, child, site, (void *)((word & ~255UL) | 0xcc)));
                CHECK(!ptrace(PTRACE_CONT, child, NULL, NULL));
                CHECK(waitpid(child, &status, 0) == child && WIFSTOPPED(status) && WSTOPSIG(status) == SIGTRAP);
                CHECK(!ptrace(PTRACE_GETREGS, child, NULL, &regs));
                CHECK(regs.rip == (uintptr_t)site + 1);
                CHECK(!ptrace(PTRACE_POKETEXT, child, site, (void *)word));
                regs.rip = (uintptr_t)site;
            } else {
                CHECK(!ptrace(PTRACE_GETREGS, child, NULL, &saved));
                regs = saved;
                regs.rip = (uintptr_t)site + 5;
                regs.rax = SYS_getpid;
                regs.orig_rax = (unsigned long)-1;
            }
            CHECK(!ptrace(PTRACE_SETREGS, child, NULL, &regs));
            for (int step = mode == 0 ? 0 : 1; step < 2; step++) {
                CHECK(!ptrace(PTRACE_SINGLESTEP, child, NULL, NULL));
                CHECK(waitpid(child, &status, 0) == child && WIFSTOPPED(status) && WSTOPSIG(status) == SIGTRAP);
                CHECK(!ptrace(PTRACE_GETREGS, child, NULL, &regs));
                CHECK(regs.rip == (uintptr_t)site + (step ? 7 : 5));
                CHECK(regs.rax == (unsigned long)(step ? child : SYS_getpid));
            }
            if (mode == 2) CHECK(!ptrace(PTRACE_SETREGS, child, NULL, &saved));
        }
        CHECK(!ptrace(PTRACE_CONT, child, NULL, NULL));
        wait_ok(child);
        verify_site(site);
    }
}
extern long core_ac_call(void *site, unsigned long *flags);
__asm__(".text\n.global core_ac_call\n.type core_ac_call,@function\n"
        "core_ac_call: pushfq; orq $0x40000,(%rsp); popfq; call *%rdi\n"
        "pushfq; pop %rdx; push %rdx; andq $-262145,(%rsp); popfq\n"
        "mov %rdx,(%rsi); ret\n.size core_ac_call,.-core_ac_call\n");
static void alignment_flags(void) {
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        unsigned long flags = 0;
        long expected = getpid();
        verify_site((void *)core_patch_site);
        CHECK(core_ac_call((void *)core_patch_site, &flags) == expected);
        CHECK(flags & (1UL << 18));
        CHECK(is_original((void *)core_patch_site));
        CHECK(core_patch_site() == expected && is_original((void *)core_patch_site));
        _exit(0);
    }
    wait_ok(child);
    verify_site((void *)core_patch_site);
}
static void restart_and_long_form(void) {
    unsigned char *p = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(p != MAP_FAILED);
    unsigned char read_code[] = {0xb8, 0, 0, 0, 0, 0x0f, 0x05, 0xc3};
    memset(p, 0x90, OFFSET);
    memcpy(p + OFFSET, read_code, sizeof(read_code));
    CHECK(!mprotect(p, page_size, PROT_READ | PROT_EXEC));
    long (*read_call)(long, void *, long) = (void *)(p + OFFSET);
    for (int restart = 0; restart < 2; restart++) {
        int pipefd[2]; char value = 0;
        CHECK(!pipe(pipefd));
        for (unsigned int n = 0; n < (hot ? hot : 1); n++) CHECK(read_call(pipefd[0], &value, 0) == 0);
        struct sigaction sa = {.sa_handler = signal_handler,
                               .sa_flags = restart ? SA_RESTART : 0};
        CHECK(!sigemptyset(&sa.sa_mask) && !sigaction(SIGUSR1, &sa, NULL));
        pid_t child = fork(); CHECK(child >= 0);
        if (!child) {
            CHECK(!usleep(100000) && !kill((pid_t)pid, SIGUSR1) && !usleep(100000));
            CHECK(write(pipefd[1], "x", 1) == 1);
            _exit(0);
        }
        long result = read_call(pipefd[0], &value, 1);
        CHECK(restart ? result == 1 && value == 'x' : result == -EINTR);
        wait_ok(child);
        CHECK(!close(pipefd[0]) && !close(pipefd[1]));
    }
    CHECK(!mprotect(p, page_size, PROT_READ | PROT_WRITE));
    unsigned char long_code[] = {0x48, 0xc7, 0xc0, 39, 0, 0, 0, 0x0f, 0x05, 0xc3};
    memcpy(p + OFFSET, long_code, sizeof(long_code));
    CHECK(!mprotect(p, page_size, PROT_READ | PROT_EXEC));
    warm((void *)(p + OFFSET), pid);
    if (enabled) CHECK(p[OFFSET] == 0xff && p[OFFSET + 7] == 0x06 && p[OFFSET + 8] == 0x06);
    else CHECK(!memcmp(p + OFFSET, long_code, sizeof(long_code)));
    CHECK(!munmap(p, page_size));
}
static void ambiguous_instructions(void) {
    static const unsigned char rex[] = {0xb8,39,0,0,0,0x45,0x31,0xc0,
        0x41,0xb8,39,0,0,0,0x0f,0x05,0x4c,0x89,0xc0,0xc3};
    static const unsigned char fs[] = {0x64,0xb8,39,0,0,0,0x0f,0x05,0xc3};
    static const unsigned char store[] = {0xb8,39,0,0,0,0xc7,0x87,0,0,0,0xb8,
        39,0,0,0,0x0f,0x05,0xc3};
    const unsigned char *codes[] = {rex, fs, store};
    const size_t sizes[] = {sizeof(rex), sizeof(fs), sizeof(store)};
    for (unsigned int i = 0; i < 3; i++) {
        unsigned char *p = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        CHECK(p != MAP_FAILED);
        memset(p, 0x90, OFFSET); memcpy(p + OFFSET, codes[i], sizes[i]);
        CHECK(!mprotect(p, page_size, PROT_READ | PROT_EXEC));
        unsigned int value = 0;
        long (*call)(uintptr_t) = (void *)(p + OFFSET);
        for (unsigned int n = 0; n < (hot ? hot + 8 : 8); n++) {
            CHECK(call((uintptr_t)&value + 0x48000000UL) == (i == 0 ? 39 : pid));
            if (i == 2) CHECK(value == 39);
            CHECK(!memcmp(p + OFFSET, codes[i], sizes[i]));
        }
        CHECK(!munmap(p, page_size));
    }
}
static void trampoline_lifetime(void) {
    if (!enabled) return;
    for (int mode = 0; mode < 3; mode++) {
        pid_t child = fork(); CHECK(child >= 0);
        if (!child) {
            void *table = (void *)0x06060000;
            if (mode == 0) {
                void *target = mmap(NULL, 65536, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                CHECK(target != MAP_FAILED);
                CHECK(mremap(table, 65536, 65536, MREMAP_MAYMOVE | MREMAP_FIXED, target) == target);
            } else if (mode == 1) {
                CHECK(!madvise(table, 65536, MADV_DONTNEED));
            } else {
                CHECK(!mprotect(table, 65536, PROT_READ | PROT_WRITE));
                CHECK(is_original((void *)core_patch_site));
                CHECK(!munmap(table, 65536));
            }
            CHECK(is_original((void *)core_patch_site));
            CHECK(core_patch_site() == getpid());
            CHECK(is_original((void *)core_patch_site));
            _exit(0);
        }
        wait_ok(child);
    }
}
int main(int argc, char **argv) {
    read_enabled();
    if (argc == 2 && !strcmp(argv[1], "fresh")) {
        CHECK(is_original((void *)core_patch_site));
        long expected = getpid();
        warm(core_patch_site, expected);
        verify_site((void *)core_patch_site);
        return 0;
    }
    CHECK(argc == 1);
    page_size = (size_t)sysconf(_SC_PAGESIZE); CHECK(page_size == 4096);
    pid = getpid();
    CHECK(is_original((void *)core_patch_site));
    warm(core_patch_site, pid);
    verify_site((void *)core_patch_site);
    char path[] = "/tmp/core-patch-XXXXXX";
    int fd = mkstemp(path); CHECK(fd >= 0 && !unlink(path));
    CHECK(!ftruncate(fd, SITES * page_size));
    for (unsigned int i = 0; i < SITES; i++) {
        unsigned char code[OFFSET + sizeof(original)];
        memset(code, 0x90, OFFSET); memcpy(code + OFFSET, original, sizeof(original));
        CHECK(pwrite(fd, code, sizeof(code), (off_t)(i * page_size)) == sizeof(code));
    }
    for (int writable = 0; writable < 2; writable++) {
        unsigned char *shared = mmap(NULL, page_size,
            PROT_READ | PROT_EXEC | (writable ? PROT_WRITE : 0),
            writable ? MAP_PRIVATE : MAP_SHARED, fd, 0);
        CHECK(shared != MAP_FAILED && is_original(shared + OFFSET));
        warm((void *)(shared + OFFSET), pid);
        CHECK(is_original(shared + OFFSET));
        CHECK(!munmap(shared, page_size));
    }
    mapping = mmap(NULL, SITES * page_size, PROT_READ | PROT_EXEC, MAP_PRIVATE, fd, 0);
    CHECK(mapping != MAP_FAILED);
    struct sigaction sa = {.sa_handler = signal_handler, .sa_flags = SA_RESTART};
    CHECK(!sigemptyset(&sa.sa_mask) && !sigaction(SIGUSR1, &sa, NULL));
    CHECK(!pthread_barrier_init(&gate, NULL, 5));
    pthread_t threads[4];
    for (unsigned int i = 0; i < 4; i++) CHECK(!pthread_create(&threads[i], NULL, worker, (void *)(uintptr_t)i));
    for (unsigned int site = 0; site < SITES; site++) {
        barrier();
        /* The fifth thread sends signals while all four first execute this page. */
        for (unsigned int n = 0; n < 16; n++)
            for (unsigned int i = 0; i < 4; i++)
                CHECK(!syscall(SYS_tgkill, pid, atomic_load(&tids[i]), SIGUSR1));
        barrier();
        verify_site(mapping + site * page_size + OFFSET);
    }
    for (unsigned int i = 0; i < 4; i++) CHECK(!pthread_join(threads[i], NULL));
    CHECK(atomic_load(&done) == 4 && atomic_load(&delivered) > 0);
    CHECK(!pthread_barrier_destroy(&gate));
    for (unsigned int i = 0; i < SITES; i++) {
        unsigned char data[sizeof(original)];
        CHECK(pread(fd, data, sizeof(data), (off_t)(i * page_size + OFFSET)) == sizeof(data));
        CHECK(!memcmp(data, original, sizeof(data)));
    }
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        long expected = getpid();
        verify_site((void *)core_patch_site);
        CHECK(core_patch_site() == expected);
        for (unsigned int i = 0; i < SITES; i++) {
            long (*call)(void) = (void *)(mapping + i * page_size + OFFSET);
            verify_site((void *)call); CHECK(call() == expected);
        }
        _exit(0);
    }
    wait_ok(child);
    child = fork(); CHECK(child >= 0);
    if (!child) { execl("/proc/self/exe", "core_patch", "fresh", (char *)NULL); _exit(127); }
    wait_ok(child);
    ptrace_after_patch();
    debugger_writes();
    alignment_flags();
    restart_and_long_form();
    ambiguous_instructions();
    trampoline_lifetime();
    void *target = mmap(NULL, SITES * page_size, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(target != MAP_FAILED);
    size_t length = (SITES - 1) * page_size + 1;
    mapping = mremap(mapping, length, length, MREMAP_MAYMOVE | MREMAP_FIXED, target);
    CHECK(mapping == target);
    for (unsigned int i = 0; i < SITES; i++) {
        unsigned char *site = mapping + i * page_size + OFFSET;
        CHECK(is_original(site));
        warm((void *)site, pid);
        verify_site(site);
    }
    CHECK(!madvise(mapping, page_size, MADV_DONTNEED));
    CHECK(is_original(mapping + OFFSET));
    warm((void *)(mapping + OFFSET), pid);
    verify_site(mapping + OFFSET);
    CHECK(mmap(mapping, page_size, PROT_READ | PROT_EXEC, MAP_PRIVATE | MAP_FIXED, fd, 0) == mapping);
    CHECK(is_original(mapping + OFFSET));
    warm((void *)(mapping + OFFSET), pid);
    verify_site(mapping + OFFSET);
    CHECK(!mprotect(mapping, 1, PROT_READ | PROT_WRITE));
    CHECK(is_original(mapping + OFFSET));
    uint32_t replacement_nr = SYS_getppid;
    memcpy(mapping + OFFSET + 1, &replacement_nr, sizeof(replacement_nr));
    CHECK(!mprotect(mapping, page_size, PROT_READ | PROT_EXEC));
    CHECK(((long (*)(void))(mapping + OFFSET))() == getppid());
    CHECK(!mprotect(mapping, page_size, PROT_READ | PROT_WRITE));
    unsigned char replacement[] = {0xb8, 123, 0, 0, 0, 0xc3, 0x90, 0x90};
    memcpy(mapping + OFFSET, replacement, sizeof(replacement));
    CHECK(!mprotect(mapping, page_size, PROT_READ | PROT_EXEC));
    CHECK(((long (*)(void))(mapping + OFFSET))() == 123);
    CHECK(!munmap(mapping, SITES * page_size) && !close(fd));
    printf("{\"schema\":\"hd.core-patch/1\",\"enabled\":%s,\"sites\":%d,\"calls\":%d,"
           "\"signals\":%u,\"fork\":true,\"exec_clean\":true,\"file_clean\":true,"
           "\"ptrace\":true,\"poketext\":true,\"proc_mem\":true,\"single_step\":true,\"alignment_flags\":true,\"restart\":true,\"long_form\":true,\"ambiguous_fallback\":true,\"mremap\":true,\"trampoline_lifetime\":%s,\"discard_remap_write\":true,\"verified\":true}\n",
           enabled ? "true" : "false", SITES, SITES * CALLS * 4, atomic_load(&delivered), enabled ? "true" : "false");
    return 0;
}
