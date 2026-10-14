/*
 * NVIDIA 550.120 NV_ESC_ALLOC_OS_EVENT / FREE_OS_EVENT / RM_GET_EVENT_DATA
 * standalone native ABI regression probe.
 *
 * Purpose: pin the exact OS-event ABI semantics that the HardDinD GPU
 * mapping must reproduce, before another cuInit run.  This probe talks
 * directly to the GPU character device with ioctl(2); it never dlopens
 * CUDA, never allocates GPU memory, never triggers notifications and
 * never touches another client's handles or any global detach/reset.
 *
 * Pinned 550.120 evidence (arkclaude/reference/nvidia-550.120):
 *   - nv_ioctl.h:69-84  nv_ioctl_alloc_os_event_t / free_os_event_t:
 *       { NvHandle hClient; NvHandle hDevice; NvU32 fd; NvU32 Status; }
 *       16 bytes.  ALLOC/FREE are escape numbers 206/207 ('F').
 *   - escape.c:679-703 dispatches ALLOC_OS_EVENT/FREE_OS_EVENT; a wrong
 *       payload size fails the SYSCALL (ioctl rc -1), while RM failures
 *       are returned through the embedded Status field with ioctl rc 0.
 *   - osapi.c:464-532 allocate_os_event():
 *       * fd is an OPAQUE TAG, not a descriptor: it is only compared for
 *         equality; CUDA passes its GPU character-device fd number.
 *       * the event list is global per nvidia control state, keyed by
 *         (hParent==hClient, fd); a duplicate pair fails inside
 *         allocate_os_event() with NV_ERR_INVALID_ARGUMENT (0x1F), and
 *         RmAllocOsEvent() (osapi.c:523-535) wraps every failure as
 *         NV_ERR_INSUFFICIENT_RESOURCES (0x1A).
 *       * freeing an unknown pair returns NV_ERR_INVALID_EVENT (0x28).
 *       * hDevice is never read.
 *   - escape.c:705-719 + osapi.c:648-680: RM_GET_EVENT_DATA (0x52,
 *       NVOS41_PARAMETERS 16 bytes) on an empty per-fd event queue
 *       returns embedded NV_ERR_OPERATING_SYSTEM (0x59) with ioctl rc 0
 *       and leaves MoreEvents at 0.
 *   - nv.c:2196-2241 nvidia_poll: on an empty queue poll() blocks until
 *       timeout and returns 0 events; O_NONBLOCK skips the wait queue.
 *   - nvstatuscodes.h: NV_OK 0, INSUFFICIENT_RESOURCES 0x1A,
 *       INVALID_ARGUMENT 0x1F, INVALID_EVENT 0x28,
 *       OPERATING_SYSTEM 0x59.
 *
 * NV0005 (NV01_EVENT, cl0005.h) notification allocation/triggering is
 * NOT exercised here: it requires an exact hSrcResource/class/notifyIndex
 * plus a real interrupt-producing trigger, which cannot be expressed
 * without fabricating an event.  It is reported as not_tested.
 *
 * Output is one JSON object per line.  Every case echoes its exact
 * request fields, the raw syscall rc/errno and the embedded Status, so a
 * strict checker can distinguish a syscall failure from an RM-level
 * embedded status.  All resources (events, NV01_ROOT clients, fds) are
 * owned and released on every branch.
 *
 * --layout-selfcheck prints compile-time struct offsets and exits
 * without touching any device (GPU-free host layout fixture).
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

/* ---- pinned 550.120 ABI structures ------------------------------------- */

/* nv-ioctl.h:69-75 */
struct nv_os_event {
    uint32_t hClient;
    uint32_t hDevice;
    uint32_t tag;                 /* pinned name: fd; opaque equality tag */
    uint32_t Status;
};
/* nvos.h:1955-1959 NVOS41_PARAMETERS */
struct nv_os41 {
    uint64_t pEvent;
    uint32_t MoreEvents;
    uint32_t status;
};
/* nvos.h NVOS64_PARAMETERS (NV01_ROOT alloc) / NVOS00_PARAMETERS (free),
 * same layout the existing gpu_alloc_probe.c pins. */
struct nv64 {                          /* nvos.h NVOS64_PARAMETERS */
    uint32_t root, parent, object, cls;
    uint64_t alloc;
    uint64_t rights;
    uint32_t size, flags, status;
};
struct nv00 {
    uint32_t root, parent, object, status;
};
struct nv_short15 { char b[15]; };

_Static_assert(sizeof(struct nv_os_event) == 16, "alloc_os_event 16");
_Static_assert(offsetof(struct nv_os_event, hClient) == 0, "hClient@0");
_Static_assert(offsetof(struct nv_os_event, hDevice) == 4, "hDevice@4");
_Static_assert(offsetof(struct nv_os_event, tag) == 8, "tag@8");
_Static_assert(offsetof(struct nv_os_event, Status) == 12, "Status@12");
_Static_assert(sizeof(struct nv_os41) == 16, "NVOS41 16");
_Static_assert(offsetof(struct nv_os41, pEvent) == 0, "pEvent@0");
_Static_assert(offsetof(struct nv_os41, MoreEvents) == 8, "MoreEvents@8");
_Static_assert(offsetof(struct nv_os41, status) == 12, "os41 status@12");
_Static_assert(sizeof(struct nv64) == 48 && offsetof(struct nv64, status) == 40,
               "NVOS64");
_Static_assert(sizeof(struct nv00) == 16, "NVOS00");

#define ESC_ALLOC_ROOT  _IOWR('F', 0x2b, struct nv64)
#define ESC_FREE        _IOWR('F', 0x29, struct nv00)
#define ESC_ALLOC_EVENT _IOWR('F', 206, struct nv_os_event)
#define ESC_FREE_EVENT  _IOWR('F', 207, struct nv_os_event)
#define ESC_GET_EVENT   _IOWR('F', 0x52, struct nv_os41)
#define ESC_ALLOC_EVENT_SHORT _IOWR('F', 206, struct nv_short15)

/* nvstatuscodes.h (550.120) */
#define ST_OK                        0u
#define ST_INSUFFICIENT_RESOURCES    0x1Au
#define ST_INVALID_ARGUMENT          0x1Fu
#define ST_INVALID_EVENT             0x28u
#define ST_OPERATING_SYSTEM         0x59u

static const char *device_path = "/dev/nvidia0";
static int poll_ms = 30;
static long allocations_ok, frees_ok, fds_opened, fds_closed;
static long clients_created, clients_freed, errors;

static void emit_meta(void);
static int tag_is_open(uint32_t tag);

static int64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000ll + ts.tv_nsec;
}

static void layout_selfcheck(void)
{
    printf("{\"type\":\"meta\",\"mode\":\"layout_selfcheck\",\"schema\":1,"
           "\"driver\":\"550.120\"}\n");
    printf("{\"type\":\"layout\",\"nv_os_event\":{\"size\":%zu,\"offsets\":{"
           "\"hClient\":%zu,\"hDevice\":%zu,\"tag\":%zu,\"Status\":%zu}},"
           "\"nv_os41\":{\"size\":%zu,\"offsets\":{\"pEvent\":%zu,"
           "\"MoreEvents\":%zu,\"status\":%zu}},\"nv64\":%zu,\"nv00\":%zu,"
           "\"escapes\":{\"ALLOC_OS_EVENT\":206,\"FREE_OS_EVENT\":207,"
           "\"RM_GET_EVENT_DATA\":82,\"ALLOC_ROOT\":43,\"FREE\":41},"
           "\"status\":{\"OK\":0,\"INSUFFICIENT_RESOURCES\":26,"
           "\"INVALID_ARGUMENT\":31,\"INVALID_EVENT\":40,"
           "\"OPERATING_SYSTEM\":89}}\n",
           sizeof(struct nv_os_event),
           offsetof(struct nv_os_event, hClient),
           offsetof(struct nv_os_event, hDevice),
           offsetof(struct nv_os_event, tag),
           offsetof(struct nv_os_event, Status),
           sizeof(struct nv_os41),
           offsetof(struct nv_os41, pEvent),
           offsetof(struct nv_os41, MoreEvents),
           offsetof(struct nv_os41, status),
           sizeof(struct nv64), sizeof(struct nv00));
}

static int open_device(int nonblock)
{
    int flags = O_RDWR | O_CLOEXEC | (nonblock ? O_NONBLOCK : 0);
    int fd = open(device_path, flags);
    if (fd < 0)
        fprintf(stderr, "open %s: %s\n", device_path, strerror(errno));
    else
        fds_opened++;
    return fd;
}

static void close_device(int fd)
{
    if (fd >= 0) {
        close(fd);
        fds_closed++;
    }
}

static uint32_t new_client(int fd)
{
    struct nv64 h;
    memset(&h, 0, sizeof(h));       /* cls 0 == NV01_ROOT */
    if (ioctl(fd, ESC_ALLOC_ROOT, &h) != 0 || h.status != ST_OK)
        return 0;
    clients_created++;
    return h.object;
}

static int free_client(int fd, uint32_t root)
{
    struct nv00 h = { root, root, root, 0 };
    if (ioctl(fd, ESC_FREE, &h) != 0 || h.status != ST_OK)
        return -1;
    clients_freed++;
    return 0;
}

/* One ALLOC/FREE_OS_EVENT call, fully recorded.  Returns the ioctl rc
 * (0/-1); embedded status comes back in *out_status. */
static int event_call(int fd, int nr, uint32_t client, uint32_t hdevice,
                      uint32_t tag, const char *name,
                      uint32_t expect, int *out_errno, uint32_t *out_status)
{
    struct nv_os_event h;
    int rc;
    memset(&h, 0, sizeof(h));
    h.hClient = client;
    h.hDevice = hdevice;
    h.tag = tag;
    errno = 0;
    rc = ioctl(fd, nr == 206 ? ESC_ALLOC_EVENT : ESC_FREE_EVENT, &h);
    *out_errno = errno;
    *out_status = h.Status;
    if (rc == 0) {
        if (nr == 206 && h.Status == ST_OK) allocations_ok++;
        if (nr == 207 && h.Status == ST_OK) frees_ok++;
        if (h.Status != expect) errors++;
    } else {
        errors++;                    /* syscall failures are case failures */
    }
    printf("{\"type\":\"event\",\"name\":\"%s\",\"op\":\"%s\","
           "\"request\":{\"nr\":%d,\"size\":%zu,\"hClient\":%u,"
           "\"hDevice\":%u,\"tag\":%u,\"tag_is_open_fd\":%s},"
           "\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"embeddedStatus\":%u,\"expectedEmbedded\":%u,\"match\":%s}\n",
           name, nr == 206 ? "alloc" : "free", nr, sizeof(h), client,
           hdevice, tag, tag_is_open(tag) ? "true" : "false",
           rc, errno, h.Status, expect,
           (rc == 0 && h.Status == expect) ? "true" : "false");
    return rc;
}

static int tag_is_open(uint32_t tag)
{
    DIR *d = opendir("/proc/self/fd");
    struct dirent *e;
    int found = 0;
    char buf[16];
    int n = snprintf(buf, sizeof(buf), "%u", tag);
    if (!d || n <= 0)
        return -1;                   /* unknown, never assert true */
    while ((e = readdir(d)) != NULL) {
        if (strlen(e->d_name) == (size_t)n && !strcmp(e->d_name, buf)) {
            found = 1;
            break;
        }
    }
    closedir(d);
    return found;
}

static void case_get_event_data_empty(int fd, uint32_t client)
{
    struct nv_os41 h;
    unsigned char out[16];
    int rc, en;
    memset(&h, 0, sizeof(h));
    memset(out, 0, sizeof(out));
    h.pEvent = (uintptr_t)out;
    errno = 0;
    rc = ioctl(fd, ESC_GET_EVENT, &h);
    en = errno;
    int out_touched = memcmp(out, (unsigned char[16]){0}, 16) != 0;
    int match = (rc == 0 && en == 0 && h.status == ST_OPERATING_SYSTEM
                 && h.MoreEvents == 0 && !out_touched);
    if (!match)
        errors++;
    printf("{\"type\":\"get_event_data\",\"name\":\"get_event_data_empty\","
           "\"request\":{\"nr\":82,\"size\":%zu,\"hClient\":%u,"
           "\"pEvent_valid\":true},\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"embeddedStatus\":%u,\"expectedEmbedded\":89,"
           "\"moreEvents\":%u,\"output_buffer_touched\":%s,\"match\":%s}\n",
           sizeof(h), client, rc, en, h.status, h.MoreEvents,
           out_touched ? "true" : "false", match ? "true" : "false");
}

static void case_poll(int fd, const char *name, int nonblock,
                      int timeout_ms)
{
    struct pollfd pfd;
    int64_t t0, t1;
    int n;
    memset(&pfd, 0, sizeof(pfd));
    pfd.fd = fd;
    pfd.events = POLLIN | POLLPRI;
    t0 = now_ns();
    n = poll(&pfd, 1, nonblock ? 0 : timeout_ms);
    t1 = now_ns();
    int match = (n == 0 && (pfd.revents & (POLLIN | POLLPRI)) == 0);
    if (!match)
        errors++;
    printf("{\"type\":\"poll\",\"name\":\"%s\",\"nonblock\":%s,"
           "\"timeout_ms\":%d,\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"revents\":%d,\"elapsed_ns\":%lld,\"empty\":%s}\n",
           name, nonblock ? "true" : "false", timeout_ms, n, errno,
           pfd.revents, (long long)(t1 - t0), match ? "true" : "false");
}

static void case_wrong_size(int fd, uint32_t client, uint32_t tag)
{
    struct nv_short15 h;
    int rc, en;
    memset(&h, 0, sizeof(h));
    /* Encode the same hClient prefix; the 16th byte (Status) is absent.
     * The driver rejects the size BEFORE dispatch, so this is a syscall
     * failure with no embedded Status and no event registration. */
    memcpy(h.b, &client, 4);
    errno = 0;
    rc = ioctl(fd, ESC_ALLOC_EVENT_SHORT, &h);
    en = errno;
    int match = (rc == -1 && en == EINVAL);
    if (!match)
        errors++;
    printf("{\"type\":\"wrong_size\",\"name\":\"wrong_size_channel\","
           "\"request\":{\"nr\":206,\"size\":15,\"hClient\":%u,\"tag\":%u},"
           "\"syscall\":{\"rc\":%d,\"errno\":%d,\"errno_name\":\"%s\"},"
           "\"embeddedStatus\":null,\"expect_syscall_errno\":22,"
           "\"embedded_channel_absent\":true,\"match\":%s}\n",
           client, tag, rc, en, en == EINVAL ? "EINVAL" : "other",
           match ? "true" : "false");
}

int main(int argc, char **argv)
{
    int fd1 = -1, fd2 = -1, fd3 = -1;
    setvbuf(stdout, NULL, _IOLBF, 0);
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--layout-selfcheck") && i == 1 && argc == 2) {
            layout_selfcheck();
            return 0;
        }
        if (!strcmp(argv[i], "--device") && i + 1 < argc)
            device_path = argv[++i];
        else if (!strcmp(argv[i], "--poll-ms") && i + 1 < argc) {
            poll_ms = atoi(argv[++i]);
            if (poll_ms < 5 || poll_ms > 1000)
                return 2;
        } else
            return 2;
    }

    /* CUDA uses the numeric GPU character-device fd as the opaque tag.
     * High tags are never descriptors in this process. */
    const uint32_t TAG_OPAQUE_E = 0x4f530001u;
    const uint32_t TAG_OPAQUE_FG = 0x4f530002u;
    const uint32_t TAG_OPAQUE_H = 0x4f530003u;

    fd1 = open_device(0);
    fd2 = open_device(0);
    fd3 = open_device(1);
    if (fd1 < 0 || fd2 < 0 || fd3 < 0) {
        emit_meta();
        printf("{\"type\":\"fatal\",\"stage\":\"open_device\"}\n");
        close_device(fd1); close_device(fd2); close_device(fd3);
        return 1;
    }
    emit_meta();
    printf("{\"type\":\"tags\",\"gpu_char_fd\":%d,\"opaque\":[%u,%u,%u],"
           "\"opaque_are_open_fds\":[%s,%s,%s]}\n",
           fd1, TAG_OPAQUE_E, TAG_OPAQUE_FG, TAG_OPAQUE_H,
           tag_is_open(TAG_OPAQUE_E) ? "true" : "false",
           tag_is_open(TAG_OPAQUE_FG) ? "true" : "false",
           tag_is_open(TAG_OPAQUE_H) ? "true" : "false");

    struct { int fd; uint32_t client; } clients[9];
    int nclients = 0;
    uint32_t gpu_tag = (uint32_t)fd1;   /* exactly what CUDA passes */

    /* Helper: create a tracked client. */
    #define MKCLIENT(FD) do { \
        uint32_t _c = new_client(FD); \
        if (!_c) { printf("{\"type\":\"error\",\"stage\":\"new_client\"}\n"); \
                  errors++; } \
        clients[nclients].fd = (FD); clients[nclients].client = _c; \
        nclients++; } while (0)

    /* A: valid alloc/free, tag == gpu char fd (CUDA behavior). */
    MKCLIENT(fd1);
    {
        int en; uint32_t st;
        event_call(fd1, 206, clients[0].client, 0, gpu_tag,
                   "alloc_free", ST_OK, &en, &st);
        event_call(fd1, 207, clients[0].client, 0, gpu_tag,
                   "alloc_free", ST_OK, &en, &st);
    }

    /* B: duplicate allocation on the same (client,tag): embedded 0x1A,
     *    ioctl rc 0; one matching free releases the single event. */
    MKCLIENT(fd1);
    {
        int en; uint32_t st;
        event_call(fd1, 206, clients[1].client, 0, gpu_tag,
                   "duplicate_alloc", ST_OK, &en, &st);
        event_call(fd1, 206, clients[1].client, 0, gpu_tag,
                   "duplicate_alloc", ST_INSUFFICIENT_RESOURCES, &en, &st);
        event_call(fd1, 207, clients[1].client, 0, gpu_tag,
                   "duplicate_alloc", ST_OK, &en, &st);
    }

    /* C: free of a pair that was never registered: embedded 0x28. */
    MKCLIENT(fd1);
    {
        int en; uint32_t st;
        event_call(fd1, 207, clients[2].client, 0, gpu_tag,
                   "missing_free", ST_INVALID_EVENT, &en, &st);
    }

    /* D: valid alloc, free, second free (double free): embedded 0x28. */
    MKCLIENT(fd1);
    {
        int en; uint32_t st;
        event_call(fd1, 206, clients[3].client, 0, gpu_tag,
                   "double_free", ST_OK, &en, &st);
        event_call(fd1, 207, clients[3].client, 0, gpu_tag,
                   "double_free", ST_OK, &en, &st);
        event_call(fd1, 207, clients[3].client, 0, gpu_tag,
                   "double_free", ST_INVALID_EVENT, &en, &st);
    }

    /* E: opaque tag that is NOT any open fd: still alloc/free OK. */
    MKCLIENT(fd1);
    {
        int en; uint32_t st;
        event_call(fd1, 206, clients[4].client, 0, TAG_OPAQUE_E,
                   "opaque_tag_not_fd", ST_OK, &en, &st);
        event_call(fd1, 207, clients[4].client, 0, TAG_OPAQUE_E,
                   "opaque_tag_not_fd", ST_OK, &en, &st);
    }

    /* F/G: two independent clients on independent nvfp (two opens),
     * same opaque tag registered on each: both succeed. */
    MKCLIENT(fd1);
    MKCLIENT(fd2);
    {
        int en; uint32_t st;
        event_call(fd1, 206, clients[5].client, 0, TAG_OPAQUE_FG,
                   "two_independent_clients", ST_OK, &en, &st);
        event_call(fd2, 206, clients[6].client, 0, TAG_OPAQUE_FG,
                   "two_independent_clients", ST_OK, &en, &st);
        event_call(fd1, 207, clients[5].client, 0, TAG_OPAQUE_FG,
                   "two_independent_clients", ST_OK, &en, &st);
        event_call(fd2, 207, clients[6].client, 0, TAG_OPAQUE_FG,
                   "two_independent_clients", ST_OK, &en, &st);
    }

    /* H: hDevice is ignored by the implementation; bogus value, OK. */
    MKCLIENT(fd1);
    {
        int en; uint32_t st;
        event_call(fd1, 206, clients[7].client, 0xDEADBEEFu, TAG_OPAQUE_H,
                   "hdevice_ignored", ST_OK, &en, &st);
        event_call(fd1, 207, clients[7].client, 0, TAG_OPAQUE_H,
                   "hdevice_ignored", ST_OK, &en, &st);
    }

    /* I: empty-queue GET_EVENT_DATA and char-fd poll semantics on a
     *    fresh client/fd, plus the wrong-size syscall-channel case. */
    MKCLIENT(fd1);
    case_get_event_data_empty(fd1, clients[8].client);
    case_poll(fd1, "poll_blocking_empty", 0, poll_ms);
    case_poll(fd3, "poll_nonblock_empty", 1, 0);
    case_wrong_size(fd1, clients[8].client, TAG_OPAQUE_H);

    /* Exact owned cleanup: free every client we created.  All OS events
     * were already released inside their cases. */
    for (int i = 0; i < nclients; i++) {
        if (clients[i].client &&
            free_client(clients[i].fd, clients[i].client) != 0) {
            printf("{\"type\":\"error\",\"stage\":\"free_client\",\"client\":"
                   "%u}\n", clients[i].client);
            errors++;
        }
    }
    close_device(fd1);
    close_device(fd2);
    close_device(fd3);

    printf("{\"type\":\"summary\",\"schema\":1,\"allocations_ok\":%ld,"
           "\"frees_ok\":%ld,\"clients_created\":%ld,\"clients_freed\":%ld,"
           "\"fds_opened\":%ld,\"fds_closed\":%ld,\"errors\":%ld,"
           "\"nv0005_notification\":\"not_tested\",\"internal_cuda\":0,"
           "\"status\":\"%s\"}\n",
           allocations_ok, frees_ok, clients_created, clients_freed,
           fds_opened, fds_closed, errors, errors ? "fail" : "pass");
    return errors ? 1 : 0;

#undef MKCLIENT
}

static void emit_meta(void)
{
    printf("{\"type\":\"meta\",\"schema\":1,\"mode\":\"native_abi\","
           "\"device\":\"%s\",\"gpu_char_fd_tag_semantics\":\"opaque_numeric\","
           "\"driver\":\"550.120\",\"poll_ms\":%d,"
           "\"sources\":{\"struct\":\"src/nvidia/arch/nvalloc/unix/"
           "include/nv-ioctl.h:69-84\",\"dispatch\":\"src/nvidia/arch/"
           "nvalloc/unix/src/escape.c:679-719\",\"event\":\"src/nvidia/"
           "arch/nvalloc/unix/src/osapi.c:464-535,648-680\","
           "\"poll\":\"kernel-open/nvidia/nv.c:2196-2241\","
           "\"status\":\"kernel-open/common/inc/nvstatuscodes.h\"}}\n",
           device_path, poll_ms);
}
