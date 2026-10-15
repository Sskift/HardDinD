/* SPDX-License-Identifier: GPL-2.0
 * Exercise extracted production GPU forwarding functions with deterministic
 * pthread schedules. Kernel user access, allocation, FD lookup and worker
 * transport are shims; this is not a GPU or guest-kernel acceptance test.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "model_constants.inc"

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
#define __user
#define GFP_KERNEL 0
#define UM_KERN_PAGE_SIZE 4096UL
#define PAGE_MASK (~(UM_KERN_PAGE_SIZE - 1))
#define TASK_SIZE (1UL << 47)
#define U64_MAX UINT64_MAX
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define max_t(t, a, b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define STUB_GPU_SHADOW 0x100000000UL

struct mm_struct {
    struct { pthread_mutex_t gpu_lock; unsigned long gpu_shadow_pages; } context;
};
struct gpu_file { struct mm_struct *mm; int host_fd, index; };
struct file { const void *f_op; void *private_data; atomic_int refs, released; };
static const int gpu_fops, other_fops;
static struct mm_struct mm, foreign_mm;
static struct gpu_file state, old_state, new_state;
static struct file old_file, new_file;
static struct file *fd_table[8];
static pthread_mutex_t fd_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t gate_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_cond = PTHREAD_COND_INITIALIZER;
static unsigned char shared[STUB_GPU_SHADOW_PAGES * UM_KERN_PAGE_SIZE];
static unsigned char aliased[STUB_GPU_SHADOW_PAGES * UM_KERN_PAGE_SIZE];
static _Thread_local int actor;
static atomic_int checks, failures, allocations, frees, worker_calls, copyouts, last_puts;
static atomic_int nested_reads, nested_writes;
static int fail_alloc, fail_in, fail_out, fail_sync, worker_rc;
static int gate, scenario, alias_negative;
static const char *case_name;

static void check(bool ok, const char *reason)
{
    atomic_fetch_add(&checks, 1);
    if (!ok) {
        atomic_fetch_add(&failures, 1);
        fprintf(stderr, "FAIL case=%s reason=%s\n", case_name, reason);
    }
}
static void must(int rc)
{
    if (rc) { fprintf(stderr, "pthread/system error=%d\n", rc); _Exit(2); }
}
static void mutex_lock(pthread_mutex_t *m) { must(pthread_mutex_lock(m)); }
static void mutex_unlock(pthread_mutex_t *m) { must(pthread_mutex_unlock(m)); }
static bool gpu_unlocked(void)
{
    int rc = pthread_mutex_trylock(&mm.context.gpu_lock);
    if (!rc) { mutex_unlock(&mm.context.gpu_lock); return true; }
    check(rc == EBUSY, "unexpected trylock result");
    return false;
}
static void wait_stage(int wanted)
{
    struct timespec until;
    must(clock_gettime(CLOCK_REALTIME, &until));
    until.tv_sec += 5;
    while (gate < wanted) must(pthread_cond_timedwait(&gate_cond, &gate_lock, &until));
}
static void stage(int value)
{
    gate = value;
    must(pthread_cond_broadcast(&gate_cond));
}
static void *kmalloc(size_t size, int flags)
{
    (void)flags;
    check(gpu_unlocked(), "allocation under GPU lock");
    if (fail_alloc) return NULL;
    void *p = alias_negative ? aliased : malloc(size);
    if (p) atomic_fetch_add(&allocations, 1);
    return p;
}
static void kfree(void *p)
{
    check(gpu_unlocked(), "free under GPU lock");
    if (p) {
        atomic_fetch_add(&frees, 1);
        if (!alias_negative) free(p);
    }
}
static int copy_from_user(void *to, const void *from, size_t size)
{
    check(gpu_unlocked(), "input user access under GPU lock");
    if (fail_in) return 1;
    memcpy(to, from, size);
    return 0;
}
static int copy_to_user(void *to, const void *from, size_t size)
{
    bool unlocked = gpu_unlocked();
    check(unlocked, "output user access under GPU lock");
    atomic_fetch_add(&copyouts, 1);
    if (scenario == 1 && actor == 1 && unlocked) {
        mutex_lock(&gate_lock);
        stage(1);                 /* A has released gpu_lock, before copying. */
        wait_stage(2);             /* B overwrites shared and finishes copyout. */
        mutex_unlock(&gate_lock);
    }
    if (fail_out) return 1;
    memcpy(to, from, size);
    return 0;
}
static bool access_ok(const void *p, u64 len)
{
    uintptr_t addr = (uintptr_t)p;
    return addr < TASK_SIZE && len <= TASK_SIZE - addr;
}
static void fault_in_readable(const void *p, u64 len)
{
    (void)p; (void)len;
    check(gpu_unlocked(), "nested input fault under GPU lock");
    atomic_fetch_add(&nested_reads, 1);
}
static void fault_in_safe_writeable(const void *p, u64 len)
{
    (void)p; (void)len;
    check(gpu_unlocked(), "nested output fault under GPU lock");
    atomic_fetch_add(&nested_writes, 1);
}
static struct file *fget(int fd)
{
    struct file *f = NULL;
    mutex_lock(&fd_lock);
    if (fd >= 0 && fd < 8) f = fd_table[fd];
    if (f) atomic_fetch_add(&f->refs, 1);
    mutex_unlock(&fd_lock);
    return f;
}
static void fput(struct file *f)
{
    int refs = atomic_fetch_sub(&f->refs, 1);
    check(refs > 0, "FD reference underflow");
    if (refs == 1) {
        check(gpu_unlocked(), "last FD put under GPU lock");
        if (scenario == 2)
            check(atomic_load(&copyouts) == 1, "held FD released before copyback");
        check(atomic_fetch_add(&f->released, 1) == 0, "duplicate file release");
        atomic_fetch_add(&last_puts, 1);
    }
}
static int um_tlb_sync(struct mm_struct *m)
{
    check(m == &mm && !gpu_unlocked(), "mapping sync not serialized");
    return fail_sync ? -EIO : 0;
}
static long gpu_command(struct mm_struct *m, int op, int fd, unsigned cmd,
                        unsigned long addr, void *payload, size_t size)
{
    (void)fd; (void)cmd; (void)payload; (void)size;
    check(m == &mm && !gpu_unlocked(), "worker not serialized");
    check(op == GPU_OP_IOCTL_ADDR && addr == STUB_GPU_SHADOW, "wrong worker command");
    atomic_fetch_add(&worker_calls, 1);
    if (scenario == 2) {
        int translated;
        memcpy(&translated, shared, sizeof(translated));
        check(translated == 77, "worker did not receive held original FD");
        mutex_lock(&gate_lock);
        stage(1);
        wait_stage(2);
        mutex_unlock(&gate_lock);
        check(atomic_load(&old_file.refs) == 1, "close/reuse lost held FD");
        check(atomic_load(&old_file.released) == 0, "file released while worker used it");
    }
    if (scenario == 3) {
        u64 pointer;
        memcpy(&pointer, shared, sizeof(pointer));
        check(pointer == U64_MAX, "invalid nested pointer not rewritten");
    }
    shared[8] ^= 0x3c;
    return worker_rc;
}

#include "extracted.inc"

static void reset(const char *name)
{
    case_name = name;
    fail_alloc = fail_in = fail_out = fail_sync = worker_rc = 0;
    gate = scenario = actor = 0;
    atomic_store(&allocations, 0); atomic_store(&frees, 0);
    atomic_store(&worker_calls, 0); atomic_store(&copyouts, 0);
    atomic_store(&last_puts, 0); atomic_store(&nested_reads, 0); atomic_store(&nested_writes, 0);
    memset(shared, 0, sizeof(shared)); memset(fd_table, 0, sizeof(fd_table));
    old_state = (struct gpu_file){.mm = &mm, .host_fd = 77};
    new_state = (struct gpu_file){.mm = &mm, .host_fd = 88};
    old_file.f_op = new_file.f_op = &gpu_fops;
    old_file.private_data = &old_state; new_file.private_data = &new_state;
    atomic_store(&old_file.refs, 1); atomic_store(&new_file.refs, 1);
    atomic_store(&old_file.released, 0); atomic_store(&new_file.released, 0);
}
static long forward(void *arg, size_t size, const struct gpu_nested_ptr *nested,
                    unsigned nnested, const struct gpu_fd_field *fds, unsigned nfds)
{
    return gpu_forward_block(&state, 123, (unsigned long)arg, size, nested, nnested, fds, nfds);
}
static void balanced(void)
{
    check(atomic_load(&allocations) == atomic_load(&frees), "private allocation leaked");
    check(gpu_unlocked(), "GPU lock leaked");
}
struct request { int id; unsigned char bytes[32]; long rc; };
static void *forward_thread(void *opaque)
{
    struct request *r = opaque;
    actor = r->id;
    if (actor == 2) {
        mutex_lock(&gate_lock); wait_stage(1); mutex_unlock(&gate_lock);
    }
    r->rc = forward(r->bytes, sizeof(r->bytes), NULL, 0, NULL, 0);
    if (actor == 2) {
        mutex_lock(&gate_lock); stage(2); mutex_unlock(&gate_lock);
    }
    return NULL;
}
static void isolation(void)
{
    reset("forced-interleaving"); scenario = 1;
    struct request a = {.id = 1}, b = {.id = 2};
    memset(a.bytes, 0xa1, sizeof(a.bytes)); memset(b.bytes, 0xb2, sizeof(b.bytes));
    pthread_t ta, tb;
    must(pthread_create(&ta, NULL, forward_thread, &a));
    must(pthread_create(&tb, NULL, forward_thread, &b));
    must(pthread_join(ta, NULL)); must(pthread_join(tb, NULL));
    check(a.rc == 0 && b.rc == 0, "concurrent ioctl failed");
    check(a.bytes[0] == 0xa1 && a.bytes[8] == (0xa1 ^ 0x3c), "A received another request's output");
    check(b.bytes[0] == 0xb2 && b.bytes[8] == (0xb2 ^ 0x3c), "B output incorrect");
    check(atomic_load(&worker_calls) == 2, "missing worker execution");
    balanced();
}
static void *close_and_reuse(void *unused)
{
    (void)unused;
    mutex_lock(&gate_lock); wait_stage(1); mutex_unlock(&gate_lock);
    mutex_lock(&fd_lock); fd_table[5] = &new_file; mutex_unlock(&fd_lock);
    fput(&old_file);
    mutex_lock(&gate_lock); stage(2); mutex_unlock(&gate_lock);
    return NULL;
}
static void descriptor_lifetime(void)
{
    reset("close-and-FD-reuse"); scenario = 2; fd_table[5] = &old_file;
    unsigned char input[32] = {0}; int fd = 5; memcpy(input, &fd, sizeof(fd));
    struct gpu_fd_field field = {.off = 0};
    pthread_t closer; must(pthread_create(&closer, NULL, close_and_reuse, NULL));
    check(forward(input, sizeof(input), NULL, 0, &field, 1) == 0, "FD ioctl failed");
    must(pthread_join(closer, NULL)); memcpy(&fd, input, sizeof(fd));
    check(fd == 5, "host descriptor leaked into user output");
    check(atomic_load(&old_file.refs) == 0 && atomic_load(&old_file.released) == 1, "held file not released exactly once");
    check(atomic_load(&new_file.refs) == 1 && atomic_load(&new_file.released) == 0, "reused guest FD touched wrong file");
    balanced();
}
static void errors(void)
{
    for (int mode = 0; mode < 7; mode++) {
        reset("error-cleanup");
        unsigned char input[32] = {0};
        long expected = 0;
        if (mode == 0) { fail_alloc = 1; expected = -ENOMEM; }
        if (mode == 1) { fail_in = 1; expected = -EFAULT; }
        if (mode == 2) { fail_sync = 1; expected = -EIO; }
        if (mode == 3) { fail_out = 1; expected = -EFAULT; }
        if (mode == 4) { worker_rc = -EFAULT; expected = -EFAULT; }
        if (mode == 5) { worker_rc = -EINVAL; expected = -EINVAL; }
        check(forward(input, sizeof(input), NULL, 0, NULL, 0) == expected, "wrong error result");
        check(atomic_load(&worker_calls) == (mode >= 3), "worker ran after early failure or did not run");
        check(atomic_load(&copyouts) == (mode >= 3 && mode != 4), "incorrect copyback on failure");
        balanced();
    }
    reset("partial-FD-hold-failure"); fd_table[5] = &old_file;
    int input[8] = {5, 7}; struct gpu_fd_field fields[2] = {{.off = 0}, {.off = 4}};
    check(forward(input, sizeof(input), NULL, 0, fields, 2) == -EBADF, "missing second FD accepted");
    check(atomic_load(&old_file.refs) == 1 && atomic_load(&old_file.released) == 0, "partial hold leaked or double-put");
    check(atomic_load(&worker_calls) == 0 && atomic_load(&copyouts) == 0, "partial hold failure reached worker/copyback");
    balanced();
    for (int mode = 0; mode < 3; mode++) {
        reset("invalid-FD-kind-or-mm"); fd_table[5] = &old_file;
        struct gpu_fd_field field = {.off = 0, .uvm = mode == 2};
        if (mode == 0) old_file.f_op = &other_fops;
        if (mode == 1) old_state.mm = &foreign_mm;
        check(forward(input, sizeof(input), NULL, 0, &field, 1) == (mode == 1 ? -EXDEV : -EINVAL), "invalid held FD accepted");
        check(atomic_load(&old_file.refs) == 1, "invalid FD reference unbalanced");
        check(atomic_load(&worker_calls) == 0 && atomic_load(&copyouts) == 0, "invalid FD reached worker/copyback");
        balanced();
    }
}
static void nested_and_bounds(void)
{
    reset("nested-pointer-rewrite"); scenario = 3;
    u64 input[4] = {U64_MAX - 4096, 0, 0, 0};
    struct gpu_nested_ptr pointer = {.ptr_off = 0, .len_off = 0xffff, .fixed_len = 8, .writable = true};
    check(forward(input, sizeof(input), &pointer, 1, NULL, 0) == 0, "rewritten pointer ioctl failed");
    check(input[0] == U64_MAX - 4096, "original pointer not restored");
    check(atomic_load(&nested_reads) == 0 && atomic_load(&nested_writes) == 0, "invalid pointer was prefaulted");
    balanced();
    reset("valid-nested-prefault"); unsigned char nested[8] = {0}; input[0] = (uintptr_t)nested;
    check(forward(input, sizeof(input), &pointer, 1, NULL, 0) == 0, "valid nested ioctl failed");
    check(atomic_load(&nested_reads) == 1 && atomic_load(&nested_writes) == 1, "missing nested prefault");
    balanced();
    reset("size-bounds");
    check(forward(input, sizeof(shared) + 1, NULL, 0, NULL, 0) == -EINVAL, "oversized block accepted");
    check(atomic_load(&allocations) == 0 && atomic_load(&worker_calls) == 0, "oversize had side effects");
    check(forward(input, 0, NULL, 0, NULL, 0) == 0, "zero-size request failed");
    balanced();
}
int main(int argc, char **argv)
{
    if (argc > 2 || (argc == 2 && strcmp(argv[1], "--alias-negative"))) return 2;
    alias_negative = argc == 2;
    must(pthread_mutex_init(&mm.context.gpu_lock, NULL));
    mm.context.gpu_shadow_pages = (unsigned long)shared;
    state = (struct gpu_file){.mm = &mm, .host_fd = 9};
    isolation();
    if (!alias_negative) { descriptor_lifetime(); errors(); nested_and_bounds(); }
    must(pthread_mutex_destroy(&mm.context.gpu_lock));
    printf("{\"checks\":%d,\"failures\":%d,\"alias_negative\":%s}\n", atomic_load(&checks), atomic_load(&failures), alias_negative ? "true" : "false");
    return atomic_load(&failures) ? 1 : 0;
}
