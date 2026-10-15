/*
 * NVIDIA 550.120 NV_ESC_ALLOC_OS_EVENT / FREE_OS_EVENT / RM_GET_EVENT_DATA
 * standalone native ABI regression probe (v2).
 *
 * Purpose: pin the exact OS-event ABI semantics the HardDinD GPU mapping
 * must reproduce.  Raw ioctl(2) against the NVIDIA character devices; no
 * CUDA dlopen, no GPU memory, no notification trigger, no other client's
 * handles, no global detach/reset.
 *
 * Device chain (pinned escape.c:373-449,417-423): NV01_ROOT_CLIENT
 * allocation (NV_ESC_RM_ALLOC 0x2b) and free (NV_ESC_RM_FREE 0x29) are
 * NV_CTL_DEVICE_ONLY, so the RM root client is created on /dev/nvidiactl.
 * ALLOC_OS_EVENT/FREE_OS_EVENT (escapes 206/207) have no ctl-only gate:
 * they take the hClient from that root client plus the calling nvfp and
 * an OPAQUE numeric tag (CUDA passes its GPU character-device fd number;
 * it is never dereferenced and is not an eventfd).
 *
 * Pinned 550.120 evidence is listed in REPORT-EVENT-v2.md.
 *
 * Output: one JSON object per line with raw syscall rc/errno AND embedded
 * RM Status as separate channels.  Owned NV01_ROOT clients/events/fds are
 * released on every branch; if root allocation fails the dependent cases
 * are explicitly skipped rather than issued with hClient 0.
 *
 * --layout-selfcheck is GPU-free.
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

/* nv-ioctl.h:69-84 */
struct nv_os_event {
    uint32_t hClient;
    uint32_t hDevice;
    uint32_t tag;
    uint32_t Status;
};
/* nvos.h:1955-1959 NVOS41_PARAMETERS */
struct nv_os41 {
    uint64_t pEvent;
    uint32_t MoreEvents;
    uint32_t status;
};
/* nvos.h NVOS64_PARAMETERS / NVOS00_PARAMETERS */
struct nv64 {
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
               "NVOS64 48 status@40");
_Static_assert(offsetof(struct nv64, object) == 8, "NVOS64 hObjectNew@8");
_Static_assert(sizeof(struct nv00) == 16, "NVOS00 16");

#define ESC_ALLOC_ROOT  _IOWR('F', 0x2b, struct nv64)
#define ESC_FREE        _IOWR('F', 0x29, struct nv00)
#define ESC_ALLOC_EVENT _IOWR('F', 206, struct nv_os_event)
#define ESC_FREE_EVENT  _IOWR('F', 207, struct nv_os_event)
#define ESC_GET_EVENT   _IOWR('F', 0x52, struct nv_os41)
#define ESC_ALLOC_EVENT_SHORT _IOWR('F', 206, struct nv_short15)

/* nvstatuscodes.h (550.120) */
#define ST_OK                        0u
#define ST_INSUFFICIENT_RESOURCES    0x1Au  /* duplicate pair, wrapped */
#define ST_INVALID_EVENT             0x28u  /* free of unknown pair */
#define ST_OPERATING_SYSTEM         0x59u  /* empty event queue */

static const char *gpu_device_path = "/dev/nvidia0";
static const char *ctl_device_path = "/dev/nvidiactl";
static int poll_ms = 30;
static long allocations_ok, frees_ok, fds_opened, fds_closed;
static long clients_created, clients_freed, events_skipped, errors;

static void emit_meta(void);

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
           "\"escapes\":{\"ALLOC_ROOT\":43,\"FREE\":41,"
           "\"ALLOC_OS_EVENT\":206,\"FREE_OS_EVENT\":207,"
           "\"RM_GET_EVENT_DATA\":82},\"device_chain\":{"
           "\"root_alloc\":\"/dev/nvidiactl NV_CTL_DEVICE_ONLY\","
           "\"os_event\":\"gpu char node, opaque numeric tag\"},"
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

/* ---- fd / tag helpers --------------------------------------------------- */

/* Tri-state: 1 open, 0 not open, -1 unknown (read /proc failed). */
static int tag_is_open(uint32_t tag)
{
    DIR *d = opendir("/proc/self/fd");
    struct dirent *e;
    int found = 0;
    char buf[16];
    int n = snprintf(buf, sizeof(buf), "%u", tag);
    if (!d || n <= 0)
        return -1;
    while ((e = readdir(d)) != NULL) {
        if (strlen(e->d_name) == (size_t)n && !strcmp(e->d_name, buf)) {
            found = 1;
            break;
        }
    }
    closedir(d);
    return found;
}

static const char *tri(int v)
{
    return v < 0 ? "null" : (v ? "true" : "false");
}

static int open_device(const char *path, int nonblock, const char *role)
{
    int flags = O_RDWR | O_CLOEXEC | (nonblock ? O_NONBLOCK : 0);
    int saved, fd = open(path, flags);
    saved = errno;
    if (fd >= 0)
        fds_opened++;
    else
        errors++;
    printf("{\"type\":\"open\",\"role\":\"%s\",\"node\":\"%s\","
           "\"nonblock\":%s,\"request_flags\":%d,\"syscall\":{\"rc\":%d,"
           "\"errno\":%d},\"fd\":%d}\n",
           role, path, nonblock ? "true" : "false", flags,
           fd >= 0 ? 0 : -1, saved, fd >= 0 ? fd : -1);
    if (fd < 0)
        fprintf(stderr, "open %s: %s\n", path, strerror(saved));
    return fd;
}

static void close_device(int fd, const char *role)
{
    int saved;
    if (fd < 0)
        return;
    errno = 0;
    close(fd);
    saved = errno;
    fds_closed++;
    printf("{\"type\":\"close\",\"role\":\"%s\",\"fd\":%d,"
           "\"syscall\":{\"errno\":%d}}\n", role, fd, saved);
}

/* ---- RM root client: raw NVOS64 alloc / NVOS00 free on ctl ------------- */

/* Returns the generated handle in hObjectNew; 0 on failure.  Every raw
 * field and both error channels are recorded. */
static uint32_t root_client_alloc(int ctl, const char *case_name, int idx)
{
    struct nv64 h;
    int saved, rc;
    memset(&h, 0, sizeof(h));   /* root=parent=object=0,cls=NV01_ROOT(0) */
    errno = 0;
    rc = ioctl(ctl, ESC_ALLOC_ROOT, &h);
    saved = errno;
    printf("{\"type\":\"root_alloc\",\"case\":\"%s\",\"index\":%d,"
           "\"node\":\"/dev/nvidiactl\",\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"embeddedStatus\":%u,\"returned\":{\"root\":%u,\"parent\":%u,"
           "\"object\":%u,\"cls\":%u,\"allocParms\":%llu,\"rights\":%llu,"
           "\"paramsSize\":%u,\"flags\":%u},\"client_handle_field\":"
           "\"object\",\"client_handle\":%u,\"match\":%s}\n",
           case_name, idx, rc, saved, h.status, h.root, h.parent, h.object,
           h.cls, (unsigned long long)h.alloc, (unsigned long long)h.rights,
           h.size, h.flags, h.object,
           (rc == 0 && h.status == ST_OK && h.object != 0) ? "true"
                                                            : "false");
    if (rc == 0 && h.status == ST_OK && h.object != 0) {
        clients_created++;
        return h.object;
    }
    errors++;
    return 0;
}

static int root_client_free(int ctl, uint32_t client, const char *case_name)
{
    struct nv00 h = { client, client, client, 0 };
    int saved, rc;
    errno = 0;
    rc = ioctl(ctl, ESC_FREE, &h);
    saved = errno;
    printf("{\"type\":\"root_free\",\"case\":\"%s\",\"client\":%u,"
           "\"node\":\"/dev/nvidiactl\",\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"embeddedStatus\":%u,\"returned\":{\"root\":%u,\"parent\":%u,"
           "\"object\":%u},\"match\":%s}\n",
           case_name, client, rc, saved, h.status, h.root, h.parent,
           h.object, (rc == 0 && h.status == ST_OK) ? "true" : "false");
    if (rc == 0 && h.status == ST_OK) {
        clients_freed++;
        return 0;
    }
    errors++;
    return -1;
}

/* ---- OS event channel --------------------------------------------------- */

static int event_call(int fd, int nr, uint32_t client, uint32_t hdevice,
                      uint32_t tag, const char *name,
                      uint32_t expect)
{
    struct nv_os_event h;
    int saved, rc, tag_state;
    memset(&h, 0, sizeof(h));
    h.hClient = client;
    h.hDevice = hdevice;
    h.tag = tag;
    errno = 0;
    rc = ioctl(fd, nr == 206 ? ESC_ALLOC_EVENT : ESC_FREE_EVENT, &h);
    saved = errno;               /* capture before /proc lookup/printf */
    tag_state = tag_is_open(tag);
    printf("{\"type\":\"event\",\"name\":\"%s\",\"op\":\"%s\","
           "\"request\":{\"nr\":%d,\"size\":%zu,\"hClient\":%u,"
           "\"hDevice\":%u,\"tag\":%u,\"tag_is_open_fd\":%s},"
           "\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"embeddedStatus\":%u,\"expectedEmbedded\":%u,\"match\":%s}\n",
           name, nr == 206 ? "alloc" : "free", nr, sizeof(h), client,
           hdevice, tag, tri(tag_state), rc, saved, h.Status, expect,
           (rc == 0 && h.Status == expect && tag_state >= 0)
               ? "true" : "false");
    if (rc == 0) {
        if (nr == 206 && h.Status == ST_OK) allocations_ok++;
        if (nr == 207 && h.Status == ST_OK) frees_ok++;
        if (h.Status != expect || tag_state < 0) errors++;
    } else {
        errors++;
    }
    return rc;
}

static void skip_case(const char *name, const char *reason)
{
    events_skipped++;
    errors++;
    printf("{\"type\":\"skip\",\"name\":\"%s\",\"reason\":\"%s\"}\n",
           name, reason);
}

/* GET_EVENT_DATA on this nvfp's empty queue.  The 16-byte output buffer
 * is pre-filled with a nonzero sentinel so "driver left it untouched" is
 * an exact byte comparison rather than a zero-vs-uninitialized guess. */
static void case_get_event_data_empty(int fd)
{
    struct nv_os41 h;
    unsigned char out[16];
    unsigned char sentinel[16];
    int saved, rc, touched;
    memset(out, 0xA5, sizeof(out));
    memset(sentinel, 0xA5, sizeof(sentinel));
    memset(&h, 0, sizeof(h));
    h.pEvent = (uintptr_t)out;
    errno = 0;
    rc = ioctl(fd, ESC_GET_EVENT, &h);
    saved = errno;
    touched = memcmp(out, sentinel, sizeof(out)) != 0;
    int match = (rc == 0 && saved == 0 && h.status == ST_OPERATING_SYSTEM
                 && h.MoreEvents == 0 && !touched);
    if (!match) errors++;
    printf("{\"type\":\"get_event_data\",\"name\":\"get_event_data_empty\","
           "\"request\":{\"nr\":82,\"size\":%zu,\"pEvent_valid\":true,"
           "\"output_sentinel\":\"a5\"},\"syscall\":{\"rc\":%d,\"errno\":%d},"
           "\"embeddedStatus\":%u,\"expectedEmbedded\":89,"
           "\"moreEvents\":%u,\"output_buffer_touched\":%s,\"match\":%s}\n",
           sizeof(h), rc, saved, h.status, h.MoreEvents,
           touched ? "true" : "false", match ? "true" : "false");
}

/* POSIX poll(2) takes the timeout from the argument regardless of the
 * fd's O_NONBLOCK flag: a timeout of 0 returns immediately on any fd, and
 * a positive timeout waits on both blocking and nonblocking fds.  Prove
 * all four combinations on a queue with no events. */
static void case_poll(int fd, const char *name, int timeout_ms)
{
    struct pollfd pfd;
    int saved_flags, saved_rc, saved_errno, n;
    int nonblock_flag;
    int64_t t0, t1;
    memset(&pfd, 0, sizeof(pfd));
    pfd.fd = fd;
    pfd.events = POLLIN | POLLPRI;
    errno = 0;
    saved_flags = fcntl(fd, F_GETFL);
    t0 = now_ns();
    n = poll(&pfd, 1, timeout_ms);
    saved_rc = n;
    saved_errno = errno;       /* poll may leave errno set on timeout=0 */
    t1 = now_ns();
    nonblock_flag = saved_flags >= 0 && (saved_flags & O_NONBLOCK);
    int empty = (n == 0 && (pfd.revents & (POLLIN | POLLPRI)) == 0);
    if (!empty || saved_flags < 0) errors++;
    printf("{\"type\":\"poll\",\"name\":\"%s\",\"timeout_ms\":%d,"
           "\"fd_nonblock\":%s,\"fd_flags\":%d,\"syscall\":{\"rc\":%d,"
           "\"errno\":%d},\"revents\":%d,\"elapsed_ns\":%lld,\"empty\":%s}\n",
           name, timeout_ms, nonblock_flag ? "true" : "false",
           saved_flags, saved_rc, saved_errno, pfd.revents,
           (long long)(t1 - t0), empty ? "true" : "false");
}

static void case_wrong_size(int fd, uint32_t client, uint32_t tag)
{
    struct nv_short15 h;
    int saved, rc;
    memset(&h, 0, sizeof(h));
    memcpy(h.b, &client, 4);
    errno = 0;
    rc = ioctl(fd, ESC_ALLOC_EVENT_SHORT, &h);
    saved = errno;
    int match = (rc == -1 && saved == EINVAL);
    if (!match) errors++;
    printf("{\"type\":\"wrong_size\",\"name\":\"wrong_size_channel\","
           "\"request\":{\"nr\":206,\"size\":15,\"hClient\":%u,\"tag\":%u},"
           "\"syscall\":{\"rc\":%d,\"errno\":%d,\"errno_name\":\"%s\"},"
           "\"embeddedStatus\":null,\"expect_syscall_errno\":22,"
           "\"embedded_channel_absent\":true,\"match\":%s}\n",
           client, tag, rc, saved, saved == EINVAL ? "EINVAL" : "other",
           match ? "true" : "false");
}

int main(int argc, char **argv)
{
    int ctl = -1, g1 = -1, g2 = -1;
    setvbuf(stdout, NULL, _IOLBF, 0);
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--layout-selfcheck") && i == 1 && argc == 2) {
            layout_selfcheck();
            return 0;
        } else if (!strcmp(argv[i], "--device") && i + 1 < argc) {
            gpu_device_path = argv[++i];
        } else if (!strcmp(argv[i], "--ctl-device") && i + 1 < argc) {
            ctl_device_path = argv[++i];
        } else if (!strcmp(argv[i], "--poll-ms") && i + 1 < argc) {
            poll_ms = atoi(argv[++i]);
            if (poll_ms < 5 || poll_ms > 1000)
                return 2;
        } else {
            fprintf(stderr,
                "usage: %s [--device /dev/nvidia0] [--ctl-device /dev/nvidiactl]"
                " [--poll-ms 5..1000] | --layout-selfcheck\n", argv[0]);
            return 2;
        }
    }

    const uint32_t TAG_E = 0x4f530001u;   /* 'O''S'.. opaque, never an fd */
    const uint32_t TAG_FG = 0x4f530002u;
    const uint32_t TAG_H = 0x4f530003u;

    emit_meta();

    ctl = open_device(ctl_device_path, 0, "ctl");
    g1 = open_device(gpu_device_path, 0, "gpu_blocking");
    g2 = open_device(gpu_device_path, 1, "gpu_nonblock");

    /* nvfp-scoped cases do not need a root client and must still run even
     * if root allocation is broken (they isolate the event queue). */
    case_poll(g1, "poll_blocking_30", poll_ms);
    case_poll(g1, "poll_blocking_0", 0);
    case_poll(g2, "poll_nonblock_30", poll_ms);
    case_poll(g2, "poll_nonblock_0", 0);
    case_get_event_data_empty(g1);
    case_wrong_size(g1, 0, TAG_H);

    if (ctl < 0 || g1 < 0 || g2 < 0) {
        printf("{\"type\":\"fatal\",\"stage\":\"open_device\"}\n");
        errors++;
        close_device(ctl, "ctl");
        close_device(g1, "gpu_blocking");
        close_device(g2, "gpu_nonblock");
        goto summary;
    }

    {
        /* Nine distinct fresh root clients, one per dependent case. */
        struct { const char *name; uint32_t c; } cl[9];
        const char *names[9] = {
            "alloc_free", "duplicate_alloc", "missing_free", "double_free",
            "opaque_tag_not_fd", "two_independent_clients_a",
            "two_independent_clients_b", "hdevice_ignored", "reserved"};
        int all = 1, i;
        for (i = 0; i < 9; i++) {
            cl[i].name = names[i];
            cl[i].c = root_client_alloc(ctl, names[i], i);
            if (!cl[i].c) all = 0;
        }
        if (!all) {
            /* No event may be issued against hClient 0: skip every
             * dependent case, free only the clients that were created. */
            static const char *deps[] = {
                "alloc_free", "duplicate_alloc", "missing_free",
                "double_free", "opaque_tag_not_fd",
                "two_independent_clients", "hdevice_ignored"};
            for (size_t k = 0; k < sizeof(deps) / sizeof(deps[0]); k++)
                skip_case(deps[k], "root_client_alloc_failed");
            for (i = 0; i < 9; i++)
                if (cl[i].c)
                    root_client_free(ctl, cl[i].c, cl[i].name);
            close_device(ctl, "ctl");
            close_device(g1, "gpu_blocking");
            close_device(g2, "gpu_nonblock");
            goto summary;
        }

        uint32_t gpu_tag = (uint32_t)g1;   /* exactly what CUDA passes */
        int en; uint32_t st;
        (void)en; (void)st;

        /* A: valid alloc/free, tag == gpu char fd. */
        event_call(g1, 206, cl[0].c, 0, gpu_tag, "alloc_free", ST_OK);
        event_call(g1, 207, cl[0].c, 0, gpu_tag, "alloc_free", ST_OK);

        /* B: duplicate pair -> embedded 0x1A, syscall rc 0; one free. */
        event_call(g1, 206, cl[1].c, 0, gpu_tag, "duplicate_alloc", ST_OK);
        event_call(g1, 206, cl[1].c, 0, gpu_tag, "duplicate_alloc",
                   ST_INSUFFICIENT_RESOURCES);
        event_call(g1, 207, cl[1].c, 0, gpu_tag, "duplicate_alloc", ST_OK);

        /* C: free of an unregistered pair -> embedded 0x28. */
        event_call(g1, 207, cl[2].c, 0, gpu_tag, "missing_free",
                   ST_INVALID_EVENT);

        /* D: alloc/free/free -> second free embedded 0x28. */
        event_call(g1, 206, cl[3].c, 0, gpu_tag, "double_free", ST_OK);
        event_call(g1, 207, cl[3].c, 0, gpu_tag, "double_free", ST_OK);
        event_call(g1, 207, cl[3].c, 0, gpu_tag, "double_free",
                   ST_INVALID_EVENT);

        /* E: opaque tag that is not any open fd. */
        event_call(g1, 206, cl[4].c, 0, TAG_E, "opaque_tag_not_fd", ST_OK);
        event_call(g1, 207, cl[4].c, 0, TAG_E, "opaque_tag_not_fd", ST_OK);

        /* F/G: two distinct root clients (from separate ctl allocations),
         * each via its own GPU nvfp, same opaque tag: both register.
         * Dedup is keyed on (hClient, tag), not nvfp. */
        event_call(g1, 206, cl[5].c, 0, TAG_FG,
                   "two_independent_clients", ST_OK);
        event_call(g2, 206, cl[6].c, 0, TAG_FG,
                   "two_independent_clients", ST_OK);
        event_call(g1, 207, cl[5].c, 0, TAG_FG,
                   "two_independent_clients", ST_OK);
        event_call(g2, 207, cl[6].c, 0, TAG_FG,
                   "two_independent_clients", ST_OK);

        /* H: hDevice ignored on alloc; free intentionally sends 0 (the
         * driver never reads it either way). */
        event_call(g1, 206, cl[7].c, 0xDEADBEEFu, TAG_H,
                   "hdevice_ignored", ST_OK);
        event_call(g1, 207, cl[7].c, 0x00000000u, TAG_H,
                   "hdevice_ignored", ST_OK);

        for (i = 0; i < 9; i++)
            root_client_free(ctl, cl[i].c, cl[i].name);
    }

    close_device(ctl, "ctl");
    close_device(g1, "gpu_blocking");
    close_device(g2, "gpu_nonblock");

summary:
    printf("{\"type\":\"summary\",\"schema\":1,\"allocations_ok\":%ld,"
           "\"frees_ok\":%ld,\"clients_created\":%ld,\"clients_freed\":%ld,"
           "\"fds_opened\":%ld,\"fds_closed\":%ld,\"cases_skipped\":%ld,"
           "\"errors\":%ld,\"nv0005_notification\":\"not_tested\","
           "\"internal_cuda\":0,\"status\":\"%s\"}\n",
           allocations_ok, frees_ok, clients_created, clients_freed,
           fds_opened, fds_closed, events_skipped, errors,
           errors ? "fail" : "pass");
    return errors ? 1 : 0;
}

static void emit_meta(void)
{
    printf("{\"type\":\"meta\",\"schema\":1,\"mode\":\"native_abi\",\"device\":\"%s\","
           "\"ctl_device\":\"%s\",\"gpu_char_fd_tag_semantics\":\"opaque_numeric\","
           "\"driver\":\"550.120\",\"poll_ms\":%d,"
           "\"device_chain\":\"NV01_ROOT alloc/free on ctl (NV_CTL_DEVICE_ONLY); "
           "OS events on gpu char nvfp with opaque numeric tag\","
           "\"sources\":{\"struct\":\"src/nvidia/arch/nvalloc/unix/include/nv-ioctl.h:69-84\","
           "\"dispatch\":\"src/nvidia/arch/nvalloc/unix/src/escape.c:373-449,679-719\","
           "\"event\":\"src/nvidia/arch/nvalloc/unix/src/osapi.c:464-535,648-680\","
           "\"poll\":\"kernel-open/nvidia/nv.c:2196-2241\","
           "\"status\":\"kernel-open/common/inc/nvstatuscodes.h\"}}\n",
           gpu_device_path, ctl_device_path, poll_ms);
}
