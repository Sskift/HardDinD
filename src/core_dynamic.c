/* Register-number wrappers: arguments, concurrent rewriting and slow calls. */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core dynamic line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define N 4096
extern unsigned char core_go_site[], core_generic_site[], core_abi0_site[];
extern long core_go_call(long, long, long, long, long, long, long);
extern long core_generic_call(long, long, long, long, long, long, long);
extern long core_abi0_call(long, long, long, long, long, long, long);
__asm__(".text\n.global core_go_call,core_go_site,core_generic_call,core_generic_site\n"
    "core_go_call: push %rbx; mov %rcx,%r11; mov %rdi,%rax; mov %rsi,%rbx\n"
    "mov %rdx,%rcx; mov %r8,%rsi; mov %r9,%r8; mov 16(%rsp),%r9; mov %r11,%rdi\n"
    ".rept 14; nop; .endr\ncore_go_site: mov %rsi,%r10; mov %rdi,%rdx\n"
    "mov %rcx,%rsi; mov %rbx,%rdi; syscall; pop %rbx; ret\n"
    "core_generic_call: mov %rdi,%rax; mov %rsi,%rdi; mov %rdx,%rsi; mov %rcx,%rdx\n"
    "mov %r8,%r10; mov %r9,%r8\n.rept 14; nop; .endr\n"
    "core_generic_site: mov 8(%rsp),%r9; syscall; ret\n"
    ".global core_abi0_call,core_abi0_site\ncore_abi0_call: push %rdi; sub $8,%rsp\n"
    "mov %rsi,%rdi; mov %rdx,%rsi; mov %rcx,%rdx; mov %r8,%r10; mov %r9,%r8\n"
    "mov 24(%rsp),%r9\n.rept 14; nop; .endr\n"
    "core_abi0_site: mov 8(%rsp),%rax; syscall; add $16,%rsp; ret\n");
typedef long (*call_fn)(long, long, long, long, long, long, long);
static call_fn calls[] = {core_go_call, core_generic_call, core_abi0_call};
static pthread_barrier_t gate;
static _Atomic unsigned int signals;
static _Atomic long tids[4];
static int enabled;
static long pid;

static void caught(int sig) { if (sig == SIGUSR1) atomic_fetch_add(&signals, 1); }
static void barrier(void) {
    int rc = pthread_barrier_wait(&gate);
    CHECK(!rc || rc == PTHREAD_BARRIER_SERIAL_THREAD);
}
static void site_check(void) {
    CHECK((core_go_site[0] == 0xff) == enabled);
    CHECK((core_generic_site[0] == 0xff) == enabled);
    CHECK((core_abi0_site[0] == 0xff) == enabled);
}
static void *worker(void *arg) {
    size_t id = (size_t)arg;
    long tid = syscall(SYS_gettid);
    atomic_store(&tids[id], tid);
    barrier();
    for (unsigned int i = 0; i < N; i++)
        for (unsigned int form = 0; form < 3; form++) {
            call_fn call = calls[form];
            CHECK(call(SYS_getpid, 1, 2, 3, 4, 5, 6) == pid);
            CHECK(call(SYS_gettid, 1, 2, 3, 4, 5, 6) == tid);
        }
    return NULL;
}
static void wait_ok(long child) {
    int status;
    CHECK(waitpid((pid_t)child, &status, 0) == child);
    CHECK(WIFEXITED(status) && !WEXITSTATUS(status));
}
int main(int argc, char **argv) {
    FILE *f = fopen("/proc/uml_syscall_patch", "r");
    if (f) {
        char key[64];
        CHECK(fscanf(f, "%63s %d", key, &enabled) == 2 && !strcmp(key, "enabled"));
        CHECK(!fclose(f));
    } else CHECK(errno == ENOENT);
    CHECK(core_go_site[0] == 0x49 && core_generic_site[0] == 0x4c && core_abi0_site[0] == 0x48);
    if (argc == 2) { CHECK(!strcmp(argv[1], "fresh")); return 0; }
    CHECK(argc == 1);
    pid = getpid();
    struct sigaction sa = {.sa_handler = caught};
    CHECK(!sigemptyset(&sa.sa_mask) && !sigaction(SIGUSR1, &sa, NULL));
    CHECK(!pthread_barrier_init(&gate, NULL, 5));
    pthread_t threads[4];
    for (size_t i = 0; i < 4; i++) CHECK(!pthread_create(&threads[i], NULL, worker, (void *)i));
    barrier();
    for (unsigned int n = 0; n < 16; n++)
        for (unsigned int i = 0; i < 4; i++) CHECK(!syscall(SYS_tgkill, pid, atomic_load(&tids[i]), SIGUSR1));
    for (unsigned int i = 0; i < 4; i++) CHECK(!pthread_join(threads[i], NULL));
    CHECK(!pthread_barrier_destroy(&gate) && atomic_load(&signals));
    site_check();
    for (unsigned int form = 0; form < 3; form++) {
        call_fn call = calls[form];
        long addr = call(SYS_mmap, 0, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        CHECK(addr > 0);
        memset((void *)addr, 0x5a, 4096);
        CHECK(!call(SYS_mprotect, addr, 4096, PROT_READ, 0, 0, 0));
        CHECK(*(unsigned char *)addr == 0x5a);
        CHECK(!call(SYS_munmap, addr, 4096, 0, 0, 0, 0));
        CHECK(call(-1, 0, 0, 0, 0, 0, 0) == -ENOSYS);
        long child = call(SYS_fork, 0, 0, 0, 0, 0, 0); CHECK(child >= 0);
        if (!child) {
            CHECK(call(SYS_getpid, 0, 0, 0, 0, 0, 0) == getpid());
            call(SYS_exit_group, 0, 0, 0, 0, 0, 0); _exit(90);
        }
        wait_ok(child);
        child = fork(); CHECK(child >= 0);
        if (!child) {
            const char *args[] = {"core_dynamic", "fresh", NULL};
            call(SYS_execve, (long)"/proc/self/exe", (long)args, (long)environ, 0, 0, 0);
            _exit(91);
        }
        wait_ok(child);
    }
    printf("{\"schema\":\"hd.core-dynamic/1\",\"enabled\":%s,\"calls\":%d,"
           "\"signals\":%u,\"arguments\":true,\"fork\":true,\"exec\":true,\"verified\":true}\n",
           enabled ? "true" : "false", N * 24, atomic_load(&signals));
    return 0;
}
