#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#define WORKERS 4
#define ROUNDS 200
#define REQUIRE(x) do { if (!(x)) { fprintf(stderr, "FAIL line=%d %s errno=%d\n", __LINE__, #x, errno); _exit(1); } } while (0)
static pthread_barrier_t barrier;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local volatile unsigned long tls_value;
static _Thread_local sigjmp_buf fault_return;
static _Thread_local volatile sig_atomic_t expect_fault;
static _Thread_local volatile sig_atomic_t caught_fault;
static volatile unsigned char *page;
static size_t page_size;
static unsigned long counter;
static atomic_int stop_writer;
static atomic_uint cow_value;
static int cpu_ids[CPU_SETSIZE], cpu_count;

static void fault_handler(int sig) {
    if (!expect_fault) _exit(128 + sig);
    caught_fault = sig;
    siglongjmp(fault_return, 1);
}
static void pin(int slot) {
    cpu_set_t cpus;
    CPU_ZERO(&cpus);
    CPU_SET(cpu_ids[slot % cpu_count], &cpus);
    REQUIRE(pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus) == 0);
}
static void rendezvous(void) {
    int rc = pthread_barrier_wait(&barrier);
    REQUIRE(rc == 0 || rc == PTHREAD_BARRIER_SERIAL_THREAD);
}
static void check_fault(int write_access) {
    caught_fault = 0;
    expect_fault = 1;
    if (!sigsetjmp(fault_return, 1)) {
        if (write_access) *page = 7;
        else { volatile unsigned char value = *page; (void)value; }
    }
    expect_fault = 0;
    REQUIRE(caught_fault == SIGSEGV);
}
static void *worker(void *arg) {
    unsigned long id = (uintptr_t)arg;
    tls_value = 0xabc000 + id;
    for (int round = 0; round < ROUNDS; round++) {
        pin((int)id + round);
        REQUIRE(tls_value == 0xabc000 + id);
        for (int i = 0; i < 100; i++) {
            REQUIRE(pthread_mutex_lock(&mutex) == 0);
            counter++;
            REQUIRE(pthread_mutex_unlock(&mutex) == 0);
        }
        rendezvous();
        if (id == 0) {
            REQUIRE(mprotect((void *)page, page_size, PROT_READ | PROT_WRITE) == 0);
            *page = (unsigned char)round;
        }
        rendezvous();
        REQUIRE(*page == (unsigned char)round);
        rendezvous();
        if (id == 0) REQUIRE(mprotect((void *)page, page_size, PROT_READ) == 0);
        rendezvous();
        check_fault(1); /* All CPUs must observe write revocation before return. */
        rendezvous();
        if (id == 0) REQUIRE(munmap((void *)page, page_size) == 0);
        rendezvous();
        check_fault(0); /* Stale mappings must not survive munmap. */
        rendezvous();
        if (id == 0) {
            void *p = mmap((void *)page, page_size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
            REQUIRE(p == (void *)page);
            *page = 0x5a;
        }
        rendezvous();
        REQUIRE(*page == 0x5a);
        rendezvous();
    }
    return (void *)(uintptr_t)tls_value;
}
/* Force complete PTE-table teardown while other CPUs install unrelated
   mappings. Distinct 2 MiB-aligned regions keep these tables separate. */
static void *mapping_churn(void *arg) {
    uintptr_t id = (uintptr_t)arg;
    size_t length = 2UL * 1024 * 1024;
    unsigned char *address = (void *)(0x500000000000UL + id * 16UL * 1024 * 1024);
    pin((int)id);
    for (int i = 0; i < ROUNDS; i++) {
        volatile unsigned char *p = mmap(address, length, PROT_READ | PROT_WRITE,
                                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
        REQUIRE(p == address);
        REQUIRE(p[0] == 0 && p[length - 1] == 0);
        p[0] = (unsigned char)id;
        p[length - 1] = (unsigned char)i;
        REQUIRE(mprotect(address, length, PROT_READ) == 0);
        REQUIRE(p[0] == id && p[length - 1] == (unsigned char)i);
        REQUIRE(munmap(address, length) == 0);
    }
    return NULL;
}

static void *writer(void *unused) {
    (void)unused;
    pin(1);
    while (!atomic_load(&stop_writer)) atomic_fetch_add(&cow_value, 1);
    return NULL;
}
int main(void) {
    cpu_set_t original;
    REQUIRE(sched_getaffinity(0, sizeof(original), &original) == 0);
    for (int cpu = 0; cpu < CPU_SETSIZE; cpu++)
        if (CPU_ISSET(cpu, &original)) cpu_ids[cpu_count++] = cpu;
    REQUIRE(cpu_count >= WORKERS);
    page_size = (size_t)sysconf(_SC_PAGESIZE);
    page = mmap(NULL, page_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    REQUIRE(page != MAP_FAILED);
    struct sigaction action = {.sa_handler = fault_handler};
    sigemptyset(&action.sa_mask);
    REQUIRE(sigaction(SIGSEGV, &action, NULL) == 0);
    REQUIRE(pthread_barrier_init(&barrier, NULL, WORKERS) == 0);
    pthread_t threads[WORKERS];
    for (uintptr_t i = 0; i < WORKERS; i++) REQUIRE(pthread_create(&threads[i], NULL, worker, (void *)i) == 0);
    for (uintptr_t i = 0; i < WORKERS; i++) {
        void *result;
        REQUIRE(pthread_join(threads[i], &result) == 0);
        REQUIRE((uintptr_t)result == 0xabc000 + i);
    }
    REQUIRE(counter == WORKERS * ROUNDS * 100UL);
    REQUIRE(munmap((void *)page, page_size) == 0);
    puts("THREAD_CHECK shared_memory_tls_futex_migration pass");
    puts("THREAD_CHECK mprotect_munmap_remap pass");
    /* Fork while another guest CPU continuously writes: parent write access
       must be revoked for COW before the child observes its private copy. */
    REQUIRE(pthread_create(&threads[0], NULL, writer, NULL) == 0);
    for (int i = 0; i < 40; i++) {
        pid_t child = fork();
        REQUIRE(child >= 0);
        if (!child) {
            unsigned int saved = atomic_load(&cow_value);
            for (int j = 0; j < 10000; j++) {
                REQUIRE(atomic_load(&cow_value) == saved);
                if (!(j % 100)) sched_yield();
            }
            atomic_store(&cow_value, 0xdeadbeefU);
            _exit(0);
        }
        int status;
        REQUIRE(waitpid(child, &status, 0) == child);
        REQUIRE(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    }
    atomic_store(&stop_writer, 1);
    REQUIRE(pthread_join(threads[0], NULL) == 0);
    REQUIRE(atomic_load(&cow_value) != 0xdeadbeefU);
    puts("THREAD_CHECK concurrent_fork_cow pass");
    for (int r = 0; r < 100; r++) {
        atomic_store(&stop_writer, 0);
        REQUIRE(pthread_create(&threads[0], NULL, writer, NULL) == 0);
        atomic_store(&stop_writer, 1);
        REQUIRE(pthread_join(threads[0], NULL) == 0);
    }
    puts("THREAD_CHECK thread_lifecycle pass");
    for (uintptr_t i = 0; i < WORKERS; i++)
        REQUIRE(pthread_create(&threads[i], NULL, mapping_churn, (void *)i) == 0);
    for (int i = 0; i < WORKERS; i++) REQUIRE(pthread_join(threads[i], NULL) == 0);
    puts("THREAD_CHECK concurrent_page_table_teardown pass");
    puts("THREAD_SEMANTICS_END status=0");
    return 0;
}
