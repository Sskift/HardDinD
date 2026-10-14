/* NVIDIA 550.120 NV_CONFIDENTIAL_COMPUTE allocation boundary probe.
 * Raw results are compared with same-host native runs, including failures.
 * This probes object allocation, not confidential-compute support or CUDA.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

struct nvos21 { uint32_t root, parent, object, cls; uint64_t alloc; uint32_t size, status; };
struct nvos64 { uint32_t root, parent, object, cls; uint64_t alloc, rights; uint32_t size, flags, status; };
struct nvos00 { uint32_t root, parent, object, status; };
union header { struct nvos21 a; struct nvos64 b; };
_Static_assert(sizeof(struct nvos21) == 32 && offsetof(struct nvos21, status) == 28, "NVOS21");
_Static_assert(sizeof(struct nvos64) == 48 && offsetof(struct nvos64, status) == 40, "NVOS64");
_Static_assert(sizeof(struct nvos00) == 16, "NVOS00");
#define ALLOC21 _IOWR('F', 0x2b, struct nvos21)
#define ALLOC64 _IOWR('F', 0x2b, struct nvos64)
#define FREE _IOWR('F', 0x29, struct nvos00)
static const char *prefix;
static int failed;
static size_t page_size;
static unsigned serial;

static void check(const char *name, int ok)
{
    printf("GPU_ALLOC_CHECK %s %s\n", name, ok ? "pass" : "fail");
    failed |= !ok;
}
static int open_ctl(void)
{
    char path[256];
    snprintf(path, sizeof(path), "%sctl", prefix);
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) perror(path);
    return fd;
}
static uint32_t client(int fd)
{
    struct nvos64 h = {0};
    return ioctl(fd, ALLOC64, &h) == 0 && h.status == 0 ? h.object : 0;
}
static int release(int fd, uint32_t root, uint32_t parent, uint32_t object)
{
    struct nvos00 h = {root, parent, object, 0};
    return ioctl(fd, FREE, &h) == 0 && h.status == 0;
}
static void hex(char *out, const void *src, size_t size)
{
    const unsigned char *p = src;
    for (size_t i = 0; i < size; i++) sprintf(out + i * 2, "%02x", p[i]);
    out[size * 2] = 0;
}
static void *pages(size_t n)
{
    void *p = mmap(NULL, n * page_size, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) { perror("mmap"); exit(2); }
    return p;
}
static void protect(void *p, size_t n, int prot)
{
    if (mprotect(p, n, prot)) { perror("mprotect"); exit(2); }
}

/* Every boundary case gets a fixed object handle, allowing cleanup even
 * when copyout to a read-only header fails after allocation has succeeded.
 * The root-client teardown is a second cleanup boundary. */
static void one(int fd, uint32_t a, uint32_t b, int wide, const char *name)
{
    unsigned char *cp = pages(2), *rp = pages(2), *hp = pages(1);
    void *child = cp, *rights = NULL;
    uint32_t child_value = a, mask = 0, declared = 0, flags = 0;
    uint32_t root = a, parent = a, object = 0x42000000u + ++serial;
    int child_readable = 1, rights_readable = 0, hprot = PROT_READ | PROT_WRITE;
    if (!strcmp(name, "auto_handle")) object = 0;
    if (!strcmp(name, "size_4")) declared = 4;
    if (!strcmp(name, "size_1")) declared = 1;
    if (!strcmp(name, "size_max")) declared = UINT32_MAX;
    if (!strcmp(name, "child_zero")) child_value = 0;
    if (!strcmp(name, "child_other_client")) child_value = b;
    if (!strcmp(name, "child_unknown")) child_value = 0xfedcba98;
    if (!strcmp(name, "root_zero") || !strcmp(name, "root_zero_bad_pointer")) root = 0;
    if (!strcmp(name, "parent_zero")) parent = 0;
    if (!strcmp(name, "parent_other")) parent = b;
    if (!strncmp(name, "cross_", 6)) child = cp + page_size - 2;
    memcpy(child, &child_value, 4);
    if (!strcmp(name, "child_null")) child = NULL, child_readable = 0;
    if (!strcmp(name, "child_low") || !strcmp(name, "root_zero_bad_pointer")) child = (void *)1, child_readable = 0;
    if (!strcmp(name, "child_noncanonical")) child = (void *)(1ull << 63), child_readable = 0;
    if (!strcmp(name, "child_wrap")) child = (void *)UINT64_MAX, child_readable = 0;
    if (!strcmp(name, "child_readonly")) protect(cp, page_size, PROT_READ);
    if (!strcmp(name, "child_none")) protect(cp, page_size, PROT_NONE), child_readable = 0;
    if (!strcmp(name, "cross_ro")) protect(cp + page_size, page_size, PROT_READ);
    if (!strcmp(name, "cross_none")) protect(cp + page_size, page_size, PROT_NONE), child_readable = 0;
    if (!strcmp(name, "header_readonly")) hprot = PROT_READ;
    if (!strcmp(name, "header_none")) hprot = PROT_NONE;
    if (!strncmp(name, "rights_", 7)) rights = rp, rights_readable = 1;
    if (!strcmp(name, "rights_all")) mask = UINT32_MAX;
    if (!strcmp(name, "rights_debug")) mask = 4;
    if (!strcmp(name, "rights_cross")) rights = rp + page_size - 2;
    if (rights) memcpy(rights, &mask, 4);
    if (!strcmp(name, "rights_low")) rights = (void *)1, rights_readable = 0;
    if (!strcmp(name, "rights_noncanonical")) rights = (void *)(1ull << 63), rights_readable = 0;
    if (!strcmp(name, "rights_readonly")) protect(rp, page_size, PROT_READ);
    if (!strcmp(name, "rights_none")) protect(rp, page_size, PROT_NONE), rights_readable = 0;
    if (!strcmp(name, "flags_2")) flags = 2;
    if (!strcmp(name, "flags_high")) flags = 0x80000000;
    if (!strcmp(name, "flags_finn")) flags = 1;
    union header h = {0};
    h.b.root = root; h.b.parent = parent; h.b.object = object;
    h.b.cls = 0xcb33; h.b.alloc = (uintptr_t)child;
    if (wide) {
        h.b.rights = (uintptr_t)rights; h.b.size = declared;
        h.b.flags = flags; h.b.status = 0xdeadbeef;
    } else { h.a.size = declared; h.a.status = 0xdeadbeef; }
    size_t hs = wide ? sizeof(h.b) : sizeof(h.a);
    memcpy(hp, &h, hs);
    char before[97], after[97], cb[9], ca[9], rb[9], ra[9];
    hex(before, &h, hs); hex(cb, &child_value, 4); hex(rb, &mask, 4);
    if (!child_readable) strcpy(cb, "n/a");
    if (!rights_readable) strcpy(rb, "n/a");
    protect(hp, page_size, hprot);
    errno = 0;
    int rc = ioctl(fd, wide ? ALLOC64 : ALLOC21, hp), err = errno;
    protect(hp, page_size, PROT_READ | PROT_WRITE);
    memcpy(&h, hp, hs); hex(after, &h, hs);
    if (child_readable) hex(ca, child, 4); else strcpy(ca, "n/a");
    if (rights_readable) hex(ra, rights, 4); else strcpy(ra, "n/a");
    printf("GPU_ALLOC_RESULT {\"case\":\"%s_%s\",\"layout\":%d,\"rc\":%d,\"errno\":%d,"
           "\"header_before\":\"%s\",\"header_after\":\"%s\","
           "\"child_before\":\"%s\",\"child_after\":\"%s\","
           "\"rights_before\":\"%s\",\"rights_after\":\"%s\"}\n",
           wide ? "v64" : "v21", name, wide ? 64 : 21, rc, err, before, after, cb, ca, rb, ra);
    if (!strcmp(name, "normal") || !strcmp(name, "auto_handle")) {
        char cn[64]; snprintf(cn, sizeof(cn), "%s_%s", wide ? "v64" : "v21", name);
        check(cn, rc == 0 && (wide ? h.b.status : h.a.status) == 0 && h.b.object != 0);
    }
    if (root == a && parent == a && h.b.object) release(fd, a, a, h.b.object);
    munmap(cp, 2 * page_size); munmap(rp, 2 * page_size); munmap(hp, page_size);
}

struct loop_args { int fd, count; uint32_t root, base; };
static void *loop(void *arg)
{
    struct loop_args *p = arg;
    for (int i = 0; i < p->count; i++) {
        uint32_t value = p->root;
        struct nvos64 h = {.root=p->root, .parent=p->root, .object=p->base+(uint32_t)i,
                          .cls=0xcb33, .alloc=(uintptr_t)&value};
        if (ioctl(p->fd, ALLOC64, &h) || h.status || value != p->root ||
            !release(p->fd, p->root, p->root, h.object)) return (void *)1;
    }
    return NULL;
}
int main(int argc, char **argv)
{
    if (argc != 3) return 2;
    prefix = argv[1]; int count = atoi(argv[2]);
    if (count < 1 || count > 1000) return 2;
    setvbuf(stdout, NULL, _IOLBF, 0); page_size = (size_t)sysconf(_SC_PAGESIZE);
    int fd = open_ctl(); check("open_control", fd >= 0); if (fd < 0) return 1;
    uint32_t a = client(fd), b = client(fd);
    check("clients", a != 0 && b != 0 && a != b); if (!a || !b) return 1;
    printf("GPU_ALLOC_IDENTITIES {\"client_a\":%u,\"client_b\":%u}\n", a, b);
    static const char *cases[] = {"normal", "auto_handle", "size_4", "size_1", "size_max",
        "child_zero", "child_other_client", "child_unknown", "root_zero", "parent_zero", "parent_other",
        "child_null", "child_low", "child_noncanonical", "child_wrap", "child_readonly", "child_none",
        "cross_rw", "cross_ro", "cross_none", "header_readonly", "header_none", "root_zero_bad_pointer"};
    static const char *extra[] = {"rights_zero", "rights_all", "rights_debug", "rights_low",
        "rights_noncanonical", "rights_readonly", "rights_none", "rights_cross", "flags_2", "flags_high", "flags_finn"};
    for (int wide = 0; wide <= 1; wide++) {
        for (size_t i = 0; i < sizeof(cases)/sizeof(*cases); i++) one(fd, a, b, wide, cases[i]);
        if (wide) for (size_t i = 0; i < sizeof(extra)/sizeof(*extra); i++) one(fd, a, b, 1, extra[i]);
    }
    pthread_t tids[4]; struct loop_args args[4]; int thread_ok = 1, started = 0;
    for (int i = 0; i < 4; i++) {
        args[i] = (struct loop_args){fd, count, a, 0x43000000u + (unsigned)i * 0x10000};
        if (pthread_create(&tids[i], NULL, loop, &args[i])) { thread_ok = 0; break; }
        started++;
    }
    for (int i = 0; i < started; i++) { void *rc; if (pthread_join(tids[i], &rc) || rc) thread_ok = 0; }
    check("four_threads_alloc_free", thread_ok);
    check("free_client_b", release(fd, b, b, b)); check("free_client_a", release(fd, a, a, a)); close(fd);
    for (int i = 0; i < 4; i++) {
        pid_t pid = fork();
        if (pid == 0) {
            int child_fd = open_ctl(); uint32_t c = child_fd < 0 ? 0 : client(child_fd);
            struct loop_args ca = {child_fd, count, c, 0x44000000};
            int ok = c && !loop(&ca);
            if (c) ok &= release(child_fd, c, c, c);
            if (child_fd >= 0) close(child_fd);
            _exit(ok ? 0 : 1);
        }
        int st = -1; char name[40]; snprintf(name, sizeof(name), "independent_process_%d", i);
        check(name, pid > 0 && waitpid(pid, &st, 0) == pid && WIFEXITED(st) && WEXITSTATUS(st) == 0);
    }
    printf("GPU_ALLOC_END status=%d internal_cuda_verified=0\n", failed);
    return failed;
}
