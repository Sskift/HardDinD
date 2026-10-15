/* NVIDIA 550.120 NV_ESC_RM_ALLOC/NV_ESC_RM_FREE root client lifecycle probe.
 *
 * Next stage after NV_ESC_REGISTER_FD: allocate and release RM NV01_ROOT(_CLIENT)
 * clients through the pinned ABI.  This is descriptor/resource lifecycle coverage,
 * not CUDA acceptance and not a device-memory test.
 *
 * ABI sources (open-gpu-kernel-modules 550.120, commit in sources.lock.json):
 *   src/nvidia/arch/nvalloc/unix/include/nv_escape.h
 *       NV_ESC_RM_FREE 0x29, NV_ESC_RM_ALLOC 0x2b (ioctl request numbers)
 *   src/common/sdk/nvidia/inc/nvos.h:159-165,454-480
 *       NVOS00 16 bytes; NVOS21 32 bytes; NVOS64 48 bytes
 *   src/nvidia/arch/nvalloc/unix/src/escape.c:373-445
 *       dataSize must be 32/48 for ALLOC, 16 for FREE; root classes are
 *       control-device only and hClass is rewritten to NV01_ROOT_CLIENT
 *   src/nvidia/arch/nvalloc/unix/src/escape.c:969
 *       RM-level failures finish the switch with rmStatus NV_OK: the ioctl
 *       returns 0 and the failure is reported in the embedded status field;
 *       only driver-level rejections (wrong device/dataSize) return -EINVAL
 *   kernel-open/nvidia/nv.c:2348-2349,2677-2680
 *       arg_size comes from _IOC_SIZE; any non-NV_OK RmIoctl result is EINVAL
 *   src/nvidia/Makefile:129 (RS_COMPATABILITY_MODE=1) and
 *   src/libraries/resserv/src/rs_server.c:1072-1082
 *       freeing an unknown object under a live client reports NV_OK
 *   src/nvidia/generated/g_system_nvoc.c:86-87 and
 *   src/nvidia/src/kernel/rmapi/client.c:722-750
 *       strict client validation: another fd's handle is NV_ERR_INVALID_CLIENT
 *   kernel-open/common/inc/nvstatuscodes.h
 *       NV_OK 0, INVALID_ARGUMENT 0x1f, INVALID_CLASS 0x22,
 *       INVALID_CLIENT 0x23, INVALID_OBJECT 0x31, INVALID_OBJECT_HANDLE 0x33
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

#if !defined(__x86_64__)
#error The pinned NVIDIA NVOS ABI layout below is validated for x86_64 only.
#endif

#define REGISTER_FD _IOWR('F', 201, int)
#define RM_REQUEST_ALLOC 0x2b
#define RM_REQUEST_FREE  0x29
#define RM_IOCTL(nr, size) _IOC(_IOC_READ | _IOC_WRITE, 'F', (nr), (size))

#define NV01_ROOT          0x00000000U
#define NV01_ROOT_NON_PRIV 0x00000001U
#define NV01_ROOT_CLIENT   0x00000041U

#define NV_OK                          0x00000000U
#define NV_ERR_INVALID_ARGUMENT        0x0000001fU
#define NV_ERR_INVALID_CLASS           0x00000022U
#define NV_ERR_INVALID_CLIENT          0x00000023U
#define NV_ERR_INVALID_OBJECT          0x00000031U
#define NV_ERR_INVALID_OBJECT_HANDLE   0x00000033U

/* nvos.h:159, 4-byte aligned legacy types on the LP64 x86_64 ABI. */
struct nvos00 {
    uint32_t hRoot;          /* [IN] client handle                         */
    uint32_t hObjectParent;  /* unused by Free                             */
    uint32_t hObjectOld;     /* [IN] object/client to free                 */
    uint32_t status;         /* [OUT] RM status                            */
};

/* nvos.h:454 */
struct nvos21 {
    uint32_t hRoot;
    uint32_t hObjectParent;
    uint32_t hObjectNew;     /* [INOUT] 0 asks the RM to generate a handle */
    uint32_t hClass;         /* [IN] rewritten to NV01_ROOT_CLIENT         */
    void    *pAllocParms;    /* NULL for root client allocation            */
    uint32_t paramsSize;
    uint32_t status;
};

/* nvos.h:469 */
struct nvos64 {
    uint32_t hRoot;
    uint32_t hObjectParent;
    uint32_t hObjectNew;
    uint32_t hClass;
    void    *pAllocParms;
    void    *pRightsRequested;
    uint32_t paramsSize;
    uint32_t flags;
    uint32_t status;
};

_Static_assert(sizeof(struct nvos00) == 16, "NVOS00_PARAMETERS is 16 bytes");
_Static_assert(sizeof(struct nvos21) == 32, "NVOS21_PARAMETERS is 32 bytes");
_Static_assert(sizeof(struct nvos64) == 48, "NVOS64_PARAMETERS is 48 bytes");
_Static_assert(offsetof(struct nvos21, pAllocParms) == 16, "");
_Static_assert(offsetof(struct nvos21, paramsSize) == 24, "");
_Static_assert(offsetof(struct nvos21, status) == 28, "");
_Static_assert(offsetof(struct nvos64, pAllocParms) == 16, "");
_Static_assert(offsetof(struct nvos64, pRightsRequested) == 24, "");
_Static_assert(offsetof(struct nvos64, paramsSize) == 32, "");
_Static_assert(offsetof(struct nvos64, flags) == 36, "");
_Static_assert(offsetof(struct nvos64, status) == 40, "");
_Static_assert(_IOC_NR(RM_IOCTL(RM_REQUEST_FREE, 16)) == 0x29, "");
_Static_assert(_IOC_SIZE(RM_IOCTL(RM_REQUEST_FREE, 16)) == 0x10, "");
_Static_assert(_IOC_SIZE(RM_IOCTL(RM_REQUEST_ALLOC, 32)) == 0x20, "");
_Static_assert(_IOC_SIZE(RM_IOCTL(RM_REQUEST_ALLOC, 48)) == 0x30, "");

static const char *prefix;
static int failed;

static void check(const char *name, int ok) {
    printf("GPU_RM_CHECK %s %s\n", name, ok ? "pass" : "fail");
    if (!ok) failed = 1;
}

/* The embedded RM status is evidence even when only nonzero/pass is asserted. */
static void rm_info(const char *name, int rc, int error, uint32_t status) {
    printf("GPU_RM_INFO %s rc=%d errno=%d rm_status=0x%08x\n", name, rc, error, status);
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
    int ok = rc == (expected_errno ? -1 : 0) && (!expected_errno || error == expected_errno)
             && arg == source;
    if (!ok) fprintf(stderr, "register target=%d source=%d rc=%d errno=%d expected=%d arg=%d\n",
                     target, source, rc, error, expected_errno, arg);
    return ok;
}

struct alloc_result {
    int rc, error;
    uint32_t handle;
    uint32_t hclass;
    uint32_t status;
};

/* variant 21 sends NVOS21 (32 bytes); anything else sends NVOS64 (48 bytes). */
static struct alloc_result rm_alloc(int fd, int variant, uint32_t hclass) {
    union {
        struct nvos21 a21;
        struct nvos64 a64;
    } args;
    memset(&args, 0, sizeof(args));
    unsigned long cmd;
    if (variant == 21) {
        args.a21.hRoot = 0;
        args.a21.hObjectParent = 0;
        args.a21.hObjectNew = 0;
        args.a21.hClass = hclass;
        cmd = RM_IOCTL(RM_REQUEST_ALLOC, sizeof(args.a21));
    } else {
        args.a64.hRoot = 0;
        args.a64.hObjectParent = 0;
        args.a64.hObjectNew = 0;
        args.a64.hClass = hclass;
        cmd = RM_IOCTL(RM_REQUEST_ALLOC, sizeof(args.a64));
    }
    errno = 0;
    int rc = ioctl(fd, cmd, &args);
    struct alloc_result result = {
        .rc = rc, .error = errno,
        .handle = variant == 21 ? args.a21.hObjectNew : args.a64.hObjectNew,
        .hclass = variant == 21 ? args.a21.hClass : args.a64.hClass,
        .status = variant == 21 ? args.a21.status : args.a64.status,
    };
    return result;
}

struct free_result { int rc, error; uint32_t status; };

static struct free_result rm_free(int fd, uint32_t root, uint32_t object) {
    struct nvos00 args;
    memset(&args, 0, sizeof(args));
    args.hRoot = root;
    args.hObjectParent = root;
    args.hObjectOld = object;
    errno = 0;
    int rc = ioctl(fd, RM_IOCTL(RM_REQUEST_FREE, sizeof(args)), &args);
    return (struct free_result){ .rc = rc, .error = errno, .status = args.status };
}

struct thread_worker {
    int ctl, gpu;
    int anchor_ok, cycles_ok;
    uint32_t anchor;
};

static void *thread_client(void *arg) {
    struct thread_worker *worker = arg;
    struct alloc_result made = rm_alloc(worker->ctl, 64, NV01_ROOT);
    worker->anchor_ok = (made.rc == 0 && made.status == NV_OK && made.handle != 0
                         && made.hclass == NV01_ROOT_CLIENT);
    worker->anchor = made.handle;
    if (!worker->anchor_ok) {
        rm_info("thread_anchor", made.rc, made.error, made.status);
        worker->cycles_ok = 0;
        return NULL;
    }
    worker->cycles_ok = 1;
    for (int i = 0; i < 16; i++) {
        struct alloc_result one = rm_alloc(worker->ctl, 64, NV01_ROOT);
        if (one.rc != 0 || one.status != NV_OK || one.handle == 0
            || one.hclass != NV01_ROOT_CLIENT) {
            rm_info("thread_cycle_alloc", one.rc, one.error, one.status);
            worker->cycles_ok = 0; break;
        }
        struct free_result gone = rm_free(worker->ctl, one.handle, one.handle);
        if (gone.rc != 0 || gone.status != NV_OK) {
            rm_info("thread_cycle_free", gone.rc, gone.error, gone.status);
            worker->cycles_ok = 0; break;
        }
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: gpu-rm-probe DEVICE_PREFIX GPU_COUNT\n"); return 2; }
    prefix = argv[1];
    int count = atoi(argv[2]);
    if (count < 1 || count > 255) return 2;
    setvbuf(stdout, NULL, _IOLBF, 0);

    int ctl = device(-1), gpu = device(0);
    check("open_control_and_gpu", ctl >= 0 && gpu >= 0);
    if (ctl < 0 || gpu < 0) return 1;

    /* Root client allocations are control-device calls; REGISTER_FD is the
     * established prerequisite for the channel, exercised here as setup. */
    check("register_gpu_control", registration(gpu, ctl, 0));
    check("duplicate_registration_rejected", registration(gpu, ctl, EINVAL));

    /* Happy path: RM-level result is the embedded status, not errno. */
    struct alloc_result first = rm_alloc(ctl, 64, NV01_ROOT);
    rm_info("alloc64_root_client", first.rc, first.error, first.status);
    check("alloc64_root_client", first.rc == 0 && first.status == NV_OK
                                 && first.handle != 0
                                 && first.hclass == NV01_ROOT_CLIENT);
    uint32_t client = first.handle;
    struct free_result freed = rm_free(ctl, client, client);
    rm_info("free_root_client", freed.rc, freed.error, freed.status);
    check("free_root_client", freed.rc == 0 && freed.status == NV_OK);

    /* Both ALLOC encodings observed from real libcuda (strace sizes 0x20/0x30)
     * must work.  NV01_ROOT_CLIENT explicit and NV01_ROOT_NON_PRIV are both
     * normalized to NV01_ROOT_CLIENT (escape.c:388-397). */
    struct alloc_result via21 = rm_alloc(ctl, 21, NV01_ROOT_CLIENT);
    rm_info("alloc21_root_client", via21.rc, via21.error, via21.status);
    int alloc21_ok = (via21.rc == 0 && via21.status == NV_OK && via21.handle != 0
                      && via21.hclass == NV01_ROOT_CLIENT);
    check("alloc21_root_client", alloc21_ok);
    struct free_result free21 = rm_free(ctl, via21.handle, via21.handle);
    rm_info("alloc21_free", free21.rc, free21.error, free21.status);
    check("alloc21_free", alloc21_ok && free21.rc == 0 && free21.status == NV_OK);

    struct alloc_result nonpriv = rm_alloc(ctl, 64, NV01_ROOT_NON_PRIV);
    rm_info("alloc64_nonpriv_client", nonpriv.rc, nonpriv.error, nonpriv.status);
    int nonpriv_ok = (nonpriv.rc == 0 && nonpriv.status == NV_OK && nonpriv.handle != 0
                      && nonpriv.hclass == NV01_ROOT_CLIENT);
    check("alloc64_nonpriv_client", nonpriv_ok);
    struct free_result freenp = rm_free(ctl, nonpriv.handle, nonpriv.handle);
    rm_info("alloc64_nonpriv_free", freenp.rc, freenp.error, freenp.status);
    check("alloc64_nonpriv_free", nonpriv_ok && freenp.rc == 0 && freenp.status == NV_OK);

    /* Two live clients on one fd get distinct generated handles. */
    struct alloc_result one = rm_alloc(ctl, 64, NV01_ROOT);
    struct alloc_result two = rm_alloc(ctl, 64, NV01_ROOT);
    int distinct = (one.rc == 0 && two.rc == 0 && one.status == NV_OK
                    && two.status == NV_OK && one.handle != 0
                    && one.handle != two.handle);
    check("independent_clients_distinct", distinct);
    struct free_result fone = rm_free(ctl, one.handle, one.handle);
    struct free_result ftwo = rm_free(ctl, two.handle, two.handle);
    check("independent_clients_freed", fone.rc == 0 && fone.status == NV_OK
                                       && ftwo.rc == 0 && ftwo.status == NV_OK);

    /* RM-level failures: ioctl 0, failure in embedded status. */
    struct alloc_result badclass = rm_alloc(ctl, 64, 0xdeadbeefU);
    rm_info("invalid_class_status", badclass.rc, badclass.error, badclass.status);
    check("invalid_class_status", badclass.rc == 0
                                  && badclass.status == NV_ERR_INVALID_CLASS);

    struct alloc_result doomed = rm_alloc(ctl, 64, NV01_ROOT);
    int doomed_ok = (doomed.rc == 0 && doomed.status == NV_OK && doomed.handle != 0);
    struct free_result done = rm_free(ctl, doomed.handle, doomed.handle);
    struct free_result again = rm_free(ctl, doomed.handle, doomed.handle);
    rm_info("double_free_rejected", again.rc, again.error, again.status);
    /* The client entry is gone: _serverFindClientEntry returns INVALID_OBJECT
     * or INVALID_OBJECT_HANDLE (rs_server.c:2321-2335); clientValidate's
     * INVALID_CLIENT cannot be reached without locking a live client. */
    check("double_free_rejected", doomed_ok && done.rc == 0
                                  && done.status == NV_OK && again.rc == 0
                                  && (again.status == NV_ERR_INVALID_OBJECT
                                      || again.status == NV_ERR_INVALID_OBJECT_HANDLE));

    struct free_result nullfree = rm_free(ctl, 0, 0);
    rm_info("free_null_client_rejected", nullfree.rc, nullfree.error, nullfree.status);
    check("free_null_client_rejected", nullfree.rc == 0 && nullfree.status != NV_OK);

    struct free_result bogus = rm_free(ctl, 0xffff0001U, 0xffff0001U);
    rm_info("free_bogus_client_rejected", bogus.rc, bogus.error, bogus.status);
    check("free_bogus_client_rejected", bogus.rc == 0 && bogus.status != NV_OK);

    /* RS_COMPATABILITY_MODE=1 (src/nvidia/Makefile:129): an unknown object
     * handle under a live client frees as NV_OK (rs_server.c:1078-1080). */
    struct alloc_result owner = rm_alloc(ctl, 64, NV01_ROOT);
    struct free_result ghost = rm_free(ctl, owner.handle, 0xdead0000U);
    rm_info("free_unknown_object_compat", ghost.rc, ghost.error, ghost.status);
    check("free_unknown_object_compat", owner.rc == 0 && owner.status == NV_OK
                                        && ghost.rc == 0
                                        && ghost.status == NV_OK);

    /* Strict client validation pins handles to the control fd they came from. */
    int ctl2 = device(-1);
    struct free_result foreign = rm_free(ctl2, owner.handle, owner.handle);
    rm_info("cross_fd_free_rejected", foreign.rc, foreign.error, foreign.status);
    check("cross_fd_free_rejected", ctl2 >= 0 && foreign.rc == 0
                                    && foreign.status == NV_ERR_INVALID_CLIENT);
    struct free_result owned = rm_free(ctl, owner.handle, owner.handle);
    rm_info("cross_fd_owner_free", owned.rc, owned.error, owned.status);
    check("cross_fd_owner_free", owned.rc == 0 && owned.status == NV_OK);
    if (ctl2 >= 0) close(ctl2);

    /* Driver-level rejections reach errno: root alloc/free are ctl-only and
     * the shim checks the encoded structure size before touching the buffer,
     * so the zeroed embedded status stays zero. */
    struct alloc_result ongpu = rm_alloc(gpu, 64, NV01_ROOT);
    rm_info("alloc_on_gpu_device_rejected", ongpu.rc, ongpu.error, ongpu.status);
    check("alloc_on_gpu_device_rejected", ongpu.rc == -1 && ongpu.error == EINVAL
                                          && ongpu.status == NV_OK);
    struct free_result freegpu = rm_free(gpu, 1, 1);
    rm_info("free_on_gpu_device_rejected", freegpu.rc, freegpu.error, freegpu.status);
    check("free_on_gpu_device_rejected", freegpu.rc == -1 && freegpu.error == EINVAL
                                         && freegpu.status == NV_OK);

    unsigned char block[64];
    memset(block, 0, sizeof(block));
    errno = 0;
    int rc = ioctl(ctl, RM_IOCTL(RM_REQUEST_ALLOC, 20), block), error = errno;
    rm_info("alloc_wrong_size_rejected", rc, error, 0);
    check("alloc_wrong_size_rejected", rc == -1 && error == EINVAL);
    memset(block, 0, sizeof(block));
    errno = 0;
    rc = ioctl(ctl, RM_IOCTL(RM_REQUEST_FREE, 32), block); error = errno;
    rm_info("free_wrong_size_rejected", rc, error, 0);
    check("free_wrong_size_rejected", rc == -1 && error == EINVAL);

    long page_size = sysconf(_SC_PAGESIZE);
    void *page = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) return 2;
    memset(page, 0, page_size);
    if (mprotect(page, page_size, PROT_NONE)) return 2;
    errno = 0;
    rc = ioctl(ctl, RM_IOCTL(RM_REQUEST_ALLOC, 48), page); error = errno;
    check("alloc_unreadable_buffer", rc == -1 && error == EFAULT);
    if (mprotect(page, page_size, PROT_READ | PROT_WRITE)) return 2;
    munmap(page, page_size);
    struct alloc_result healed = rm_alloc(ctl, 64, NV01_ROOT);
    struct free_result healfree = rm_free(ctl, healed.handle, healed.handle);
    check("recovery_after_fault", healed.rc == 0 && healed.status == NV_OK
                                  && healed.handle != 0
                                  && healfree.rc == 0
                                  && healfree.status == NV_OK);

    /* Four threads sharing one fd: concurrent generated anchors must differ
     * while live; the RM API lock and the bridge gpu_lock serialize calls. */
    pthread_t threads[4];
    struct thread_worker workers[4];
    int thread_ok = 1;
    for (int i = 0; i < 4; i++) {
        workers[i] = (struct thread_worker){ .ctl = ctl, .gpu = gpu };
        if (pthread_create(&threads[i], NULL, thread_client, &workers[i])) return 2;
    }
    for (int i = 0; i < 4; i++) {
        if (pthread_join(threads[i], NULL) || !workers[i].anchor_ok
            || !workers[i].cycles_ok) thread_ok = 0;
    }
    for (int i = 0; i < 4; i++)
        for (int j = i + 1; j < 4; j++)
            if (workers[i].anchor == 0 || workers[i].anchor == workers[j].anchor)
                thread_ok = 0;
    check("concurrent_client_handles_distinct", thread_ok);
    int anchors_freed = 1;
    for (int i = 0; i < 4; i++) {
        if (workers[i].anchor) {
            struct free_result gone = rm_free(ctl, workers[i].anchor, workers[i].anchor);
            if (gone.rc != 0 || gone.status != NV_OK) anchors_freed = 0;
        }
    }
    check("four_threads_shared_control", thread_ok && anchors_freed);

    /* Reopen lifecycle on the same channel. */
    int reopen_ok = 1;
    close(gpu); close(ctl);
    for (int i = 0; i < 8; i++) {
        ctl = device(-1); gpu = device(i % count);
        struct alloc_result made = {0};
        struct free_result gone = {0};
        if (ctl < 0 || gpu < 0 || !registration(gpu, ctl, 0)) { reopen_ok = 0; }
        if (reopen_ok) {
            made = rm_alloc(ctl, 64, NV01_ROOT);
            if (made.rc != 0 || made.status != NV_OK || made.handle == 0) reopen_ok = 0;
        }
        if (reopen_ok) {
            gone = rm_free(ctl, made.handle, made.handle);
            if (gone.rc != 0 || gone.status != NV_OK) reopen_ok = 0;
        }
        if (ctl >= 0) close(ctl);
        if (gpu >= 0) close(gpu);
        if (!reopen_ok) break;
    }
    check("reopen_lifecycle", reopen_ok);

    /* A client not freed explicitly is destroyed with its fd (osapi.c
     * rm_cleanup_file_private): after reopening, the stale handle fails. */
    int close_ok = 0;
    uint32_t stale = 0;
    ctl = device(-1); gpu = device(0);
    if (ctl >= 0 && gpu >= 0 && registration(gpu, ctl, 0)) {
        struct alloc_result leak = rm_alloc(ctl, 64, NV01_ROOT);
        close_ok = (leak.rc == 0 && leak.status == NV_OK && leak.handle != 0);
        stale = leak.handle;
    }
    if (gpu >= 0) close(gpu);
    if (ctl >= 0) close(ctl);
    int stale_rejected = 0;
    ctl = device(-1); gpu = device(0);
    if (close_ok && ctl >= 0 && gpu >= 0 && registration(gpu, ctl, 0)) {
        struct free_result late = rm_free(ctl, stale, stale);
        rm_info("close_releases_client", late.rc, late.error, late.status);
        stale_rejected = (late.rc == 0 && late.status != NV_OK);
    }
    check("close_releases_client", close_ok && stale_rejected);
    if (ctl >= 0) close(ctl);
    if (gpu >= 0) close(gpu);

    /* Independent processes reopen the devices: fd inheritance is unsupported
     * by the bridge, so every child establishes its own registration. */
    int process_ok = 1;
    for (int i = 0; i < 4; i++) {
        pid_t child = fork();
        if (child < 0) return 2;
        if (!child) {
            int cctl = device(-1), cgpu = device(i % count);
            int good = 0;
            if (cctl >= 0 && cgpu >= 0 && registration(cgpu, cctl, 0)) {
                struct alloc_result made = rm_alloc(cctl, 64, NV01_ROOT);
                if (made.rc == 0 && made.status == NV_OK && made.handle != 0
                    && made.hclass == NV01_ROOT_CLIENT) {
                    struct free_result gone = rm_free(cctl, made.handle, made.handle);
                    good = gone.rc == 0 && gone.status == NV_OK;
                }
            }
            if (cctl >= 0) close(cctl);
            if (cgpu >= 0) close(cgpu);
            _exit(good ? 0 : 1);
        }
        int status;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status)
            || WEXITSTATUS(status)) process_ok = 0;
    }
    check("independent_process_lifecycle", process_ok);

    printf("GPU_RM_END status=%d internal_cuda_verified=0\n", failed);
    return failed;
}
