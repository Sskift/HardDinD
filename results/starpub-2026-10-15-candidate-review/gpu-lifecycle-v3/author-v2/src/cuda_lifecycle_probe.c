/*
 * HardDinD CUDA lifecycle probe: multi-card, thread/process and bounded
 * memory lifecycle coverage through the CUDA driver API.
 *
 * This is a new diagnostic; it does not complete the HardDinD GPU goal.
 * UVM, memory pressure, IPC and asynchronous-copy lifecycle coverage still
 * require later suites, and global free-VRAM bytes on a shared GPU cannot
 * establish the absence of a physical leak.
 *
 * No CUDA toolkit/headers are required: libcuda.so.1 is dlopen()ed and the
 * driver entry points are resolved with dlsym().  The kernel is pinned PTX
 * ".version 7.0" / ".target sm_70", exactly the version/target already used
 * by src/cuda_probe.c (which is left unchanged); the host driver is
 * 550.120.
 *
 * Every driver signature below was checked on 2026-10-14 against the
 * primary NVIDIA CUDA 12.4 archive documentation and cross-checked against
 * the symbols exported by libcuda.so.1 from driver 550.120; see
 * docs/cuda-12.4-driver-api-signatures.txt for the verbatim prototypes:
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__INITIALIZE.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__DEVICE.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__CTX.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__CTX__DEPRECATED.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__MEM.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__MODULE.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__EXEC.html
 *   https://docs.nvidia.com/cuda/archive/12.4.0/cuda-driver-api/group__CUDA__ERROR.html
 *
 * stdout is one JSON object per line, in this fixed order:
 *   meta       schema, requested parameters, cuInit rc
 *   inventory  one record per enumerated card (ordinal, UUID, name, bytes)
 *   solo       per card, repeated context create/destroy rounds, each with
 *              bounded alloc/HtoD/kernel/DtoH/validate/free cycles whose
 *              pattern seed changes every cycle
 *   shared     >=4 pthreads on one context (the deprecated cuCtxAttach/
 *              Detach usage-count pair plus per-thread cuCtxSetCurrent),
 *              private per-thread buffers, mutex/cond start gate;
 *              monotonic intervals prove the threads coexisted
 *   multicard  one pthread per card, each with its own fresh context;
 *              gate-aligned intervals prove cross-card coexistence
 *   child      concurrent fork()+execv() children that initialize CUDA
 *              fresh after exec.  The post-fork child uses only
 *              async-signal-safe calls before execv and never touches
 *              CUDA.  A pipe gate starts the children together and the
 *              parent proves launch-before-reap and interval overlap
 *   summary    single final record; the external checker re-derives the
 *              verdict and never trusts this record's status
 *
 * Timing is traced for coexistence/lifecycle evidence only.  There is no
 * performance acceptance threshold here.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

typedef int CUresult;
typedef int CUdevice;
typedef void *CUcontext;
typedef void *CUmodule;
typedef void *CUfunction;
typedef unsigned long long CUdeviceptr;

struct cuda_api {
    CUresult (*cuInit)(unsigned);
    CUresult (*cuDeviceGetCount)(int *);
    CUresult (*cuDeviceGet)(CUdevice *, int);
    CUresult (*cuDeviceGetName)(char *, int, CUdevice);
    CUresult (*cuDeviceGetUuid)(unsigned char *, CUdevice);
    CUresult (*cuDeviceTotalMem_v2)(size_t *, CUdevice);
    CUresult (*cuCtxCreate_v2)(CUcontext *, unsigned, CUdevice);
    CUresult (*cuCtxDestroy_v2)(CUcontext);
    CUresult (*cuCtxSetCurrent)(CUcontext);
    CUresult (*cuCtxAttach)(CUcontext *, unsigned);
    CUresult (*cuCtxDetach)(CUcontext);
    CUresult (*cuCtxSynchronize)(void);
    CUresult (*cuMemAlloc_v2)(CUdeviceptr *, size_t);
    CUresult (*cuMemFree_v2)(CUdeviceptr);
    CUresult (*cuMemcpyHtoD_v2)(CUdeviceptr, const void *, size_t);
    CUresult (*cuMemcpyDtoH_v2)(void *, CUdeviceptr, size_t);
    CUresult (*cuMemGetInfo_v2)(size_t *, size_t *);
    CUresult (*cuModuleLoadData)(CUmodule *, const void *);
    CUresult (*cuModuleUnload)(CUmodule);
    CUresult (*cuModuleGetFunction)(CUfunction *, CUmodule, const char *);
    CUresult (*cuLaunchKernel)(CUfunction, unsigned, unsigned, unsigned,
                               unsigned, unsigned, unsigned, unsigned,
                               void *, void **, void **);
};

/* Pinned PTX, same .version/.target as src/cuda_probe.c. */
static const char ptx[] =
".version 7.0\n.target sm_70\n.address_size 64\n"
".visible .entry add(.param .u64 pa,.param .u64 pb,.param .u64 pc,.param .u32 pn){\n"
".reg .pred p; .reg .b32 r<6>; .reg .b64 d<8>; .reg .f32 f<4>;\n"
"ld.param.u64 d1,[pa]; ld.param.u64 d2,[pb]; ld.param.u64 d3,[pc]; ld.param.u32 r1,[pn];\n"
"mov.u32 r2,%ctaid.x; mov.u32 r3,%ntid.x; mov.u32 r4,%tid.x; mad.lo.s32 r5,r2,r3,r4;\n"
"setp.ge.u32 p,r5,r1; @p bra done; mul.wide.u32 d4,r5,4;\n"
"add.u64 d5,d1,d4; add.u64 d6,d2,d4; add.u64 d7,d3,d4;\n"
"ld.global.f32 f1,[d5]; ld.global.f32 f2,[d6]; add.f32 f3,f1,f2; st.global.f32 [d7],f3;\n"
"done: ret; }\n";

#define DEFAULT_CYCLES 8
#define DEFAULT_CHILD_CYCLES 2
#define DEFAULT_CTX_ROUNDS 2
#define DEFAULT_THREADS 4
#define DEFAULT_ELEMENTS (1u << 18)          /* 1 MiB per float array */
#define DEFAULT_DEADLINE_MS 30000
#define MAX_CYCLES 32
#define MAX_THREADS 16
#define MAX_CHILDREN 32
#define MAX_ROUNDS 4
#define MAX_GPUS 32
#define UUID_LEN 41          /* "GPU-" + 8-4-4-4-12 hex + NUL = 41 bytes */
#define NAME_LEN 256
/* Conservative, fixture-verified bounds for the child pipe report line.
 * The child fails closed (exit 9) if a report would not fit; it never
 * truncates.  tests/cuda_lifecycle_host_fixture.c proves the empirical
 * worst case (INT_MIN rcs, UINT32_MAX counters, INT64 timestamps,
 * MAX_CYCLES records: 280-byte prefix, 165-byte token) stays inside the
 * margins below.  At MAX_CYCLES a report can exceed PIPE_BUF, but each
 * child owns a private pipe with exactly one writer, so no interleaving
 * is possible and the parent's newline-delimited reader stays correct. */
#define CHILD_PREFIX_MAX 320
#define CHILD_CYCLE_TOKEN_MAX 170
#define CHILD_LINE_MAX 5808
_Static_assert(DEFAULT_CHILD_CYCLES <= MAX_CYCLES,
               "default child cycles exceed MAX_CYCLES");
_Static_assert(MAX_CHILDREN >= MAX_GPUS,
               "default one-child-per-card must stay within MAX_CHILDREN");
_Static_assert(CHILD_LINE_MAX >=
               CHILD_PREFIX_MAX + 2 +
               MAX_CYCLES * (CHILD_CYCLE_TOKEN_MAX + 1),
               "child report line buffer cannot hold MAX_CYCLES");

/* Explicit, reproducible cycle identity.  A fork()+exec child starts with
 * a fresh zero-initialized global state, so a process-wide counter would
 * repeat patterns across processes; instead every cycle is namespaced by
 * (role, process-seq, worker, round, cycle) and hashed to a seed.  No pid
 * or time entropy: identical requests produce identical patterns in every
 * run and the external checker re-derives each seed independently.
 *   proc 0 is the parent; children use proc = child seq + 1. */
#define ROLE_SOLO 1u
#define ROLE_SHARED 2u
#define ROLE_MULTICARD 3u
#define ROLE_CHILD 4u

struct config {
    int cycles;
    int child_cycles;
    int ctx_rounds;
    int threads;
    int children;
    unsigned elements;
    int deadline_ms;
    int ordinals[MAX_GPUS];
    int ngpu;
};

struct cycle_result {
    uint32_t idx;
    uint32_t seed;
    uint32_t role;
    uint32_t proc;
    uint32_t worker;
    uint32_t round;
    int alloc_rc[3];
    int htod_rc;
    int launch_rc;
    int sync_rc;
    int dtoh_rc;
    int free_rc[3];
    uint32_t mismatches;
    uint32_t csum;
    int64_t start_ns;
    int64_t end_ns;
};

struct round_result {
    int create_rc;
    int load_rc;
    int unload_rc;
    int destroy_rc;
    int info_enter_rc;
    int info_exit_rc;
    uint64_t free_enter;
    uint64_t free_exit;
    struct cycle_result cycles[MAX_CYCLES];
};

struct worker_result {
    int worker;
    int ordinal;
    char uuid[UUID_LEN];
    pid_t pid;
    pid_t tid;
    int elements;
    int cycles;
    uint32_t allocs;
    uint32_t frees;
    int attach_rc;
    int detach_rc;
    int create_rc;
    int load_rc;
    int unload_rc;
    int destroy_rc;
    int64_t ready_ns;
    int64_t start_ns;
    int64_t end_ns;
    struct cycle_result cycle[MAX_CYCLES];
};

struct child_observation {
    int seq;
    int ordinal;
    pid_t child_pid;
    char uuid[UUID_LEN];
    int received;
    int exec_errno;
    int exit_code;
    int exit_signal;
    int killed;
    pid_t reported_pid;
    pid_t reported_ppid;
    int elements;
    int cycles;
    uint32_t allocs;
    uint32_t frees;
    uint32_t mismatches;
    int create_rc, load_rc, unload_rc, destroy_rc;
    int64_t start_ns, end_ns;
    int64_t fork_ns, release_ns, firstbyte_ns, wait_ns;
    struct cycle_result cycle[MAX_CYCLES];
};

struct jb {
    char *p;
    size_t n, cap;
    int oom;
};

static const struct config default_config = {
    .cycles = DEFAULT_CYCLES,
    .child_cycles = DEFAULT_CHILD_CYCLES,
    .ctx_rounds = DEFAULT_CTX_ROUNDS,
    .threads = DEFAULT_THREADS,
    .children = -1,                      /* sentinel: max(4, card count) */
    .elements = DEFAULT_ELEMENTS,
    .deadline_ms = DEFAULT_DEADLINE_MS,
};

static struct cuda_api API;
static atomic_int global_failed;
static pthread_mutex_t emit_lock = PTHREAD_MUTEX_INITIALIZER;

/* Deterministic per-cycle pattern seed.  uint32 wrap is well defined; the
 * splitmix-style finalizer spreads the structured identity.  Mirrored by
 * cycle_seed() in scripts/gpu_lifecycle_checks.py — change both together. */
static uint32_t cycle_seed(uint32_t role, uint32_t proc, uint32_t worker,
                           uint32_t round, uint32_t cycle)
{
    uint32_t h;

    h = 0x9e3779b9u
      + role * 0x85ebca77u + proc * 0xc2b2ae3du
      + worker * 0x27d4eb2fu + round * 0x165667b1u
      + cycle * 0xd1b54a31u;
    h ^= h >> 16;
    h *= 0x7feb352du;
    h ^= h >> 15;
    h *= 0x846ca68bu;
    h ^= h >> 16;
    return h ? h : 1u;
}

/* ---- tiny growable JSON line buffer ----------------------------------- */

static void jb_init(struct jb *b, size_t cap)
{
    b->p = malloc(cap);
    b->n = 0;
    b->cap = b->p ? cap : 0;
    b->oom = !b->p;
}

static void jb_put(struct jb *b, const char *s)
{
    size_t len = strlen(s);
    if (b->oom)
        return;
    if (b->n + len + 1 > b->cap) {
        size_t cap = b->cap ? b->cap * 2 : 4096;
        char *p;
        while (cap < b->n + len + 1)
            cap *= 2;
        p = realloc(b->p, cap);
        if (!p) { b->oom = 1; return; }
        b->p = p; b->cap = cap;
    }
    memcpy(b->p + b->n, s, len);
    b->n += len;
    b->p[b->n] = '\0';
}

static void jb_printf(struct jb *b, const char *fmt, ...)
{
    char tmp[1024];
    va_list ap;
    if (b->oom)
        return;
    va_start(ap, fmt);
    vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    jb_put(b, tmp);
}

static void emit(struct jb *b)
{
    pthread_mutex_lock(&emit_lock);
    if (!b->oom) {
        fwrite(b->p, 1, b->n, stdout);
        putchar('\n');
        fflush(stdout);
    }
    pthread_mutex_unlock(&emit_lock);
    free(b->p);
}

static void record_failure(const char *stage)
{
    struct jb b;
    atomic_store(&global_failed, 1);
    jb_init(&b, 160);
    jb_printf(&b, "{\"type\":\"error\",\"stage\":\"%s\",\"errno\":%d}",
              stage, errno);
    emit(&b);
}

static void record_failuref(const char *stage, const char *fmt, ...)
{
    char msg[160];
    struct jb b;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    atomic_store(&global_failed, 1);
    jb_init(&b, 256);
    jb_printf(&b, "{\"type\":\"error\",\"stage\":\"%s\",\"message\":\"%s\"}",
              stage, msg);
    emit(&b);
}

static int64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000ll + ts.tv_nsec;
}

static int64_t now_ms(void)
{
    return now_ns() / 1000000;
}

static pid_t gettid_(void)
{
    return (pid_t)syscall(SYS_gettid);
}

static void uuid_format(const unsigned char bytes[16], char out[UUID_LEN])
{
    snprintf(out, UUID_LEN,
             "GPU-%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-"
             "%02x%02x%02x%02x%02x%02x",
             bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5],
             bytes[6], bytes[7], bytes[8], bytes[9], bytes[10], bytes[11],
             bytes[12], bytes[13], bytes[14], bytes[15]);
}

static void json_string(struct jb *b, const char *s)
{
    unsigned char c;
    jb_put(b, "\"");
    while ((c = (unsigned char)*s++)) {
        if (c == '"' || c == '\\') { jb_put(b, "\\"); jb_printf(b, "%c", c); }
        else if (c < 0x20) jb_printf(b, "\\u%04x", c);
        else jb_printf(b, "%c", c);
    }
    jb_put(b, "\"");
}

/* ---- real compute ------------------------------------------------------ */

/* Integer-valued floats: every result stays below 2^24 and is exactly
 * representable, so host and f32 device addition agree bit for bit. */
static float pattern_value(uint32_t i, uint32_t seed, int side)
{
    if (side == 0)
        return (float)((i * 7u + seed * 13u) % 1009u);
    return (float)((i * 11u + seed * 17u) % 907u + 1u);
}

/* Exact host-side validation of device output.  The device value is
 * compared against the exact f32 expected value BEFORE any float->integer
 * conversion: converting a NaN or out-of-range float to an unsigned
 * integer is undefined behavior in C, so malformed or stale device data
 * must fail deterministically without ever reaching the cast.  The
 * checksum accumulates only matched, finite, bounded, expected values.
 * ha[j], hb[j] are integer-valued in [0,1008] and [1,908], so want is an
 * integer in [1,1917]: exactly representable, non-negative and finite. */
static int validate_outputs(const float *ha, const float *hb,
                            const float *hc, unsigned elements,
                            uint32_t *mismatches_out, uint32_t *csum_out)
{
    uint32_t mismatches = 0, csum = 0;
    unsigned j;

    for (j = 0; j < elements; j++) {
        float got = hc[j];
        float want = ha[j] + hb[j];

        /* NaN != NaN already fails the equality test, but spell the
         * finite/range contract out so a non-finite or negative result
         * can never reach the conversion below. */
        if (!isfinite(got) || got < 0.f || got != want) {
            mismatches++;
            continue;
        }
        /* got == want here; want is in [1,1917], so got is a finite,
         * non-negative, in-range integer-valued float and this conversion
         * is fully defined.  Accumulate the validated bounded value. */
        csum += (uint32_t)(unsigned)got;
    }
    *mismatches_out = mismatches;
    *csum_out = csum;
    return mismatches ? 1 : 0;
}

/* Runs n cycles on the context/function already current on this thread.
 * Every device resource allocated in a cycle is freed in that same cycle;
 * the return code of every driver call, including the three frees, is
 * recorded for the external checker.  Each cycle carries an explicit
 * (role, proc, worker, round) identity so patterns cannot collide across
 * threads or across the fresh-CUDA exec children. */
static int run_cycles(CUfunction fn, unsigned elements, int n,
                      uint32_t role, uint32_t proc, uint32_t worker,
                      uint32_t round, struct cycle_result *out,
                      uint32_t *allocs, uint32_t *frees)
{
    size_t bytes = (size_t)elements * sizeof(float);
    float *ha, *hb, *hc;
    int bad = 0, i;

    ha = malloc(bytes); hb = malloc(bytes); hc = malloc(bytes);
    if (!ha || !hb || !hc) {
        free(ha); free(hb); free(hc);
        return -1;
    }
    for (i = 0; i < n; i++) {
        CUdeviceptr da = 0, db = 0, dc = 0;
        unsigned j;
        uint32_t seed;
        void *args[4];
        struct cycle_result *c = &out[i];

        memset(c, 0, sizeof(*c));
        c->idx = (uint32_t)i;
        c->role = role;
        c->proc = proc;
        c->worker = worker;
        c->round = round;
        seed = cycle_seed(role, proc, worker, round, (uint32_t)i);
        c->seed = seed;
        for (j = 0; j < elements; j++) {
            ha[j] = pattern_value(j, seed, 0);
            hb[j] = pattern_value(j, seed, 1);
        }
        c->start_ns = now_ns();
        c->alloc_rc[0] = API.cuMemAlloc_v2(&da, bytes);
        c->alloc_rc[1] = API.cuMemAlloc_v2(&db, bytes);
        c->alloc_rc[2] = API.cuMemAlloc_v2(&dc, bytes);
        if (!c->alloc_rc[0]) (*allocs)++;
        if (!c->alloc_rc[1]) (*allocs)++;
        if (!c->alloc_rc[2]) (*allocs)++;
        if (da)
            c->htod_rc = API.cuMemcpyHtoD_v2(da, ha, bytes);
        else
            c->htod_rc = -1;
        if (!c->htod_rc && db)
            c->htod_rc = API.cuMemcpyHtoD_v2(db, hb, bytes);
        if (!c->htod_rc && da && db && dc) {
            /* Block width is 256 threads; the grid divisor must match. */
            unsigned grid = (elements + 255u) / 256u;
            args[0] = &da; args[1] = &db; args[2] = &dc; args[3] = &elements;
            c->launch_rc = API.cuLaunchKernel(fn, grid, 1, 1, 256, 1, 1, 0,
                                              NULL, args, NULL);
            if (!c->launch_rc)
                c->sync_rc = API.cuCtxSynchronize();
        } else if (!c->htod_rc) {
            c->launch_rc = -1;
        } else {
            c->launch_rc = c->htod_rc;
        }
        if (!c->launch_rc && !c->sync_rc && dc)
            c->dtoh_rc = API.cuMemcpyDtoH_v2(hc, dc, bytes);
        else
            c->dtoh_rc = c->sync_rc ? c->sync_rc :
                          (c->launch_rc ? c->launch_rc : -1);
        if (!c->dtoh_rc) {
            uint32_t m = 0, s = 0;
            if (validate_outputs(ha, hb, hc, elements, &m, &s))
                bad = 1;
            c->mismatches = m;
            c->csum = s;
        } else {
            c->mismatches = UINT32_MAX;
            bad = 1;
        }
        if (da) { c->free_rc[0] = API.cuMemFree_v2(da); if (!c->free_rc[0]) (*frees)++; }
        else c->free_rc[0] = -1;
        if (db) { c->free_rc[1] = API.cuMemFree_v2(db); if (!c->free_rc[1]) (*frees)++; }
        else c->free_rc[1] = -1;
        if (dc) { c->free_rc[2] = API.cuMemFree_v2(dc); if (!c->free_rc[2]) (*frees)++; }
        else c->free_rc[2] = -1;
        c->end_ns = now_ns();
        if (c->alloc_rc[0] || c->alloc_rc[1] || c->alloc_rc[2] ||
            c->htod_rc || c->launch_rc || c->sync_rc || c->dtoh_rc ||
            c->free_rc[0] || c->free_rc[1] || c->free_rc[2])
            bad = 1;
    }
    free(ha); free(hb); free(hc);
    return bad;
}

static int load_add_kernel(CUmodule *mod, CUfunction *fn)
{
    int rc = API.cuModuleLoadData(mod, ptx);
    if (rc)
        return rc;
    rc = API.cuModuleGetFunction(fn, *mod, "add");
    if (rc)
        API.cuModuleUnload(*mod);
    return rc;
}

/* ---- JSON serialization ------------------------------------------------ */

static void put_cycles_json(struct jb *b, const char *key,
                            const struct cycle_result *c, int n)
{
    int i, j;
    jb_printf(b, "\"%s\":[", key);
    for (i = 0; i < n; i++) {
        if (i) jb_put(b, ",");
        jb_printf(b, "{\"cycle\":%u,\"seed\":%u,\"role\":%u,\"proc\":%u,"
                     "\"worker\":%u,\"round\":%u,\"alloc_rc\":[",
                  c->idx, c->seed, c->role, c->proc, c->worker, c->round);
        for (j = 0; j < 3; j++)
            jb_printf(b, "%s%d", j ? "," : "", c->alloc_rc[j]);
        jb_printf(b, "],\"htod_rc\":%d,\"launch_rc\":%d,\"sync_rc\":%d,"
                     "\"dtoh_rc\":%d,\"free_rc\":[",
                  c->htod_rc, c->launch_rc, c->sync_rc, c->dtoh_rc);
        for (j = 0; j < 3; j++)
            jb_printf(b, "%s%d", j ? "," : "", c->free_rc[j]);
        jb_printf(b, "],\"mismatches\":%u,\"csum\":%u,\"start_ns\":%lld,"
                     "\"end_ns\":%lld}",
                  c->mismatches, c->csum,
                  (long long)c->start_ns, (long long)c->end_ns);
        c++;
    }
    jb_put(b, "]");
}

static void emit_meta(const struct config *cfg, int init_rc)
{
    struct jb b;
    jb_init(&b, 256);
    jb_printf(&b, "{\"type\":\"meta\",\"schema\":1,\"init_rc\":%d,"
                  "\"cycles\":%d,\"child_cycles\":%d,\"ctx_rounds\":%d,"
                  "\"threads\":%d,\"children\":%d,\"elements\":%u,"
                  "\"pid\":%ld}",
              init_rc, cfg->cycles, cfg->child_cycles, cfg->ctx_rounds,
              cfg->threads, cfg->children, cfg->elements, (long)getpid());
    emit(&b);
}

static void emit_inventory(int ordinal, const char *uuid, const char *name,
                           uint64_t total_mem, int uuid_valid)
{
    struct jb b;
    jb_init(&b, 512);
    jb_printf(&b, "{\"type\":\"inventory\",\"ordinal\":%d,\"uuid\":", ordinal);
    json_string(&b, uuid);
    jb_printf(&b, ",\"uuid_valid\":%s,\"name\":",
              uuid_valid ? "true" : "false");
    json_string(&b, name);
    jb_printf(&b, ",\"total_mem\":%llu}", (unsigned long long)total_mem);
    emit(&b);
}

static void emit_solo_worker(int index, int ordinal, const char *uuid,
                             const struct round_result *rounds, int nrounds,
                             int cycles, unsigned elements,
                             uint32_t allocs, uint32_t frees,
                             int64_t start_ns, int64_t end_ns)
{
    struct jb b;
    int r;
    jb_init(&b, 2048);
    jb_printf(&b, "{\"type\":\"worker\",\"phase\":\"solo\",\"worker\":%d,"
                  "\"ordinal\":%d,\"uuid\":", index, ordinal);
    json_string(&b, uuid);
    jb_printf(&b, ",\"pid\":%ld,\"tid\":%ld,\"elements\":%u,\"ctx_rounds\":%d,"
                  "\"cycles\":%d,\"allocs\":%u,\"frees\":%u,\"rounds\":[",
              (long)getpid(), (long)gettid_(), elements, nrounds, cycles,
              allocs, frees);
    for (r = 0; r < nrounds; r++) {
        if (r) jb_put(&b, ",");
        jb_printf(&b, "{\"round\":%d,\"create_rc\":%d,\"load_rc\":%d,"
                      "\"unload_rc\":%d,\"destroy_rc\":%d,"
                      "\"info_enter_rc\":%d,\"info_exit_rc\":%d,"
                      "\"free_enter\":%llu,\"free_exit\":%llu,",
                  r, rounds[r].create_rc, rounds[r].load_rc,
                  rounds[r].unload_rc, rounds[r].destroy_rc,
                  rounds[r].info_enter_rc, rounds[r].info_exit_rc,
                  (unsigned long long)rounds[r].free_enter,
                  (unsigned long long)rounds[r].free_exit);
        put_cycles_json(&b, "cycles", rounds[r].cycles, cycles);
        jb_put(&b, "}");
    }
    jb_printf(&b, "],\"start_ns\":%lld,\"end_ns\":%lld}",
              (long long)start_ns, (long long)end_ns);
    emit(&b);
}

static void emit_shared_ctx(int ordinal, const char *uuid, int threads,
                            int create_rc, int load_rc, int unload_rc,
                            int destroy_rc)
{
    struct jb b;
    jb_init(&b, 512);
    jb_printf(&b, "{\"type\":\"ctx\",\"phase\":\"shared\",\"ordinal\":%d,"
                  "\"uuid\":", ordinal);
    json_string(&b, uuid);
    jb_printf(&b, ",\"threads\":%d,\"create_rc\":%d,\"load_rc\":%d,"
                  "\"unload_rc\":%d,\"destroy_rc\":%d}",
              threads, create_rc, load_rc, unload_rc, destroy_rc);
    emit(&b);
}

static void emit_thread_worker(const char *phase,
                               const struct worker_result *w)
{
    struct jb b;
    jb_init(&b, 1024);
    jb_printf(&b, "{\"type\":\"worker\",\"phase\":\"%s\",\"worker\":%d,"
                  "\"ordinal\":%d,\"uuid\":", phase, w->worker, w->ordinal);
    json_string(&b, w->uuid);
    jb_printf(&b, ",\"pid\":%ld,\"tid\":%ld,\"elements\":%d,\"cycles\":%d,"
                  "\"allocs\":%u,\"frees\":%u,",
              (long)w->pid, (long)w->tid, w->elements, w->cycles,
              w->allocs, w->frees);
    if (!strcmp(phase, "shared"))
        jb_printf(&b, "\"attach_rc\":%d,\"detach_rc\":%d,",
                  w->attach_rc, w->detach_rc);
    else
        jb_printf(&b, "\"create_rc\":%d,\"load_rc\":%d,\"unload_rc\":%d,"
                      "\"destroy_rc\":%d,",
                  w->create_rc, w->load_rc, w->unload_rc, w->destroy_rc);
    jb_printf(&b, "\"ready_ns\":%lld,\"start_ns\":%lld,\"end_ns\":%lld,",
              (long long)w->ready_ns, (long long)w->start_ns,
              (long long)w->end_ns);
    put_cycles_json(&b, "cycles_detail", w->cycle, w->cycles);
    jb_put(&b, "}");
    emit(&b);
}

static void emit_child_worker(const struct child_observation *o)
{
    struct jb b;
    jb_init(&b, 1024);
    jb_printf(&b, "{\"type\":\"worker\",\"phase\":\"child\",\"worker\":%d,"
                  "\"ordinal\":%d,\"uuid\":", o->seq, o->ordinal);
    json_string(&b, o->uuid);
    jb_printf(&b, ",\"child_pid\":%ld,\"reported_pid\":%ld,\"reported_ppid\":%ld,"
                  "\"received\":%s,\"exec_errno\":%d,\"child_exit\":%d,"
                  "\"child_signal\":%d,\"killed\":%s,\"elements\":%d,"
                  "\"cycles\":%d,\"allocs\":%u,\"frees\":%u,\"mismatches\":%u,"
                  "\"create_rc\":%d,\"load_rc\":%d,\"unload_rc\":%d,"
                  "\"destroy_rc\":%d,\"fork_ns\":%lld,\"release_ns\":%lld,"
                  "\"firstbyte_ns\":%lld,\"wait_ns\":%lld,\"start_ns\":%lld,"
                  "\"end_ns\":%lld,",
              (long)o->child_pid, (long)o->reported_pid,
              (long)o->reported_ppid, o->received ? "true" : "false",
              o->exec_errno, o->exit_code, o->exit_signal,
              o->killed ? "true" : "false", o->elements, o->cycles,
              o->allocs, o->frees, o->mismatches, o->create_rc, o->load_rc,
              o->unload_rc, o->destroy_rc, (long long)o->fork_ns,
              (long long)o->release_ns, (long long)o->firstbyte_ns,
              (long long)o->wait_ns, (long long)o->start_ns,
              (long long)o->end_ns);
    put_cycles_json(&b, "cycles_detail", o->cycle, o->cycles);
    jb_put(&b, "}");
    emit(&b);
}

/* ---- phase: solo per-card context/memory cycles ------------------------ */

static void run_solo_phase(CUdevice dev, int index, int ordinal,
                           const char *uuid, const struct config *cfg)
{
    struct round_result rounds[MAX_ROUNDS];
    uint32_t allocs = 0, frees = 0;
    int r, bad_any = 0;
    int64_t start_ns = now_ns(), end_ns;

    memset(rounds, 0, sizeof(rounds));
    for (r = 0; r < cfg->ctx_rounds; r++) {
        CUcontext ctx = NULL;
        CUmodule mod = NULL;
        CUfunction fn = NULL;
        size_t free_b = 0, total_b = 0;

        rounds[r].create_rc = API.cuCtxCreate_v2(&ctx, 0, dev);
        rounds[r].load_rc = rounds[r].unload_rc = rounds[r].destroy_rc = -1;
        rounds[r].info_enter_rc = rounds[r].info_exit_rc = -1;
        if (rounds[r].create_rc) {
            bad_any = 1;
            record_failuref("solo_create", "ordinal=%d rc=%d", ordinal,
                            rounds[r].create_rc);
            continue;
        }
        /* Free-byte counters are diagnostic only: on a shared GPU they do
         * not prove that a physical leak is absent. */
        rounds[r].info_enter_rc = API.cuMemGetInfo_v2(&free_b, &total_b);
        rounds[r].free_enter = (uint64_t)free_b;
        rounds[r].load_rc = load_add_kernel(&mod, &fn);
        if (rounds[r].load_rc) {
            bad_any = 1;
            record_failuref("solo_load", "ordinal=%d rc=%d", ordinal,
                            rounds[r].load_rc);
        } else if (run_cycles(fn, cfg->elements, cfg->cycles,
                              ROLE_SOLO, 0, (uint32_t)index, (uint32_t)r,
                              rounds[r].cycles, &allocs, &frees)) {
            bad_any = 1;
            record_failuref("solo_cycles", "ordinal=%d round=%d", ordinal, r);
        }
        free_b = total_b = 0;
        rounds[r].info_exit_rc = API.cuMemGetInfo_v2(&free_b, &total_b);
        rounds[r].free_exit = (uint64_t)free_b;
        if (mod)
            rounds[r].unload_rc = API.cuModuleUnload(mod);
        rounds[r].destroy_rc = API.cuCtxDestroy_v2(ctx);
        if (rounds[r].load_rc || rounds[r].unload_rc ||
            rounds[r].destroy_rc || rounds[r].info_enter_rc ||
            rounds[r].info_exit_rc)
            bad_any = 1;
    }
    end_ns = now_ns();
    emit_solo_worker(index, ordinal, uuid, rounds, cfg->ctx_rounds,
                     cfg->cycles, cfg->elements, allocs, frees,
                     start_ns, end_ns);
    if (bad_any)
        atomic_store(&global_failed, 1);
}

/* ---- phase: threads sharing one context -------------------------------- */

/* Start gate with a participant count fixed after pthread_create returns,
 * so a failed thread creation can never leave siblings blocked forever on
 * a barrier waiting for a thread that will never arrive. */
struct start_gate {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    int total;
    int waiting;
    int released;
};

static void gate_init(struct start_gate *g)
{
    pthread_mutex_init(&g->mu, NULL);
    pthread_cond_init(&g->cv, NULL);
    g->total = g->waiting = g->released = 0;
}

static void gate_arm(struct start_gate *g, int total)
{
    pthread_mutex_lock(&g->mu);
    g->total = total;
    if (g->waiting >= total && total > 0)
        g->released = 1;
    pthread_cond_broadcast(&g->cv);
    pthread_mutex_unlock(&g->mu);
}

static void gate_wait(struct start_gate *g)
{
    pthread_mutex_lock(&g->mu);
    g->waiting++;
    if (g->total > 0 && g->waiting >= g->total) {
        g->released = 1;
        pthread_cond_broadcast(&g->cv);
    }
    while (!g->released)
        pthread_cond_wait(&g->cv, &g->mu);
    pthread_mutex_unlock(&g->mu);
}

struct shared_args {
    CUdevice dev;
    int index;
    int ordinal;
    const char *uuid;
    CUcontext ctx;
    CUfunction fn;
    unsigned elements;
    int cycles;
    struct start_gate *gate;
    pthread_t handle;
    struct worker_result result;
};

static void *shared_thread(void *arg)
{
    struct shared_args *a = arg;
    CUcontext attached = NULL;
    uint32_t allocs = 0, frees = 0;
    struct worker_result *w = &a->result;

    memset(w, 0, sizeof(*w));
    w->worker = a->index;
    w->ordinal = a->ordinal;
    strncpy(w->uuid, a->uuid, UUID_LEN - 1);
    w->pid = getpid();
    w->tid = gettid_();
    w->elements = (int)a->elements;
    w->cycles = a->cycles;
    w->attach_rc = w->detach_rc = -1;
    /* Deprecated usage-count pair, verified against the 12.4
     * group__CUDA__CTX__DEPRECATED page; the context is then bound to this
     * thread with cuCtxSetCurrent (group__CUDA__CTX).  Both return codes
     * are recorded and checked. */
    w->attach_rc = API.cuCtxAttach(&attached, 0);
    if (!w->attach_rc)
        API.cuCtxSetCurrent(a->ctx);
    w->ready_ns = now_ns();
    gate_wait(a->gate);
    if (!w->attach_rc) {
        w->start_ns = now_ns();
        if (run_cycles(a->fn, a->elements, a->cycles, ROLE_SHARED, 0,
                       (uint32_t)a->index, 0, w->cycle, &allocs, &frees))
            atomic_store(&global_failed, 1);
        w->end_ns = now_ns();
        w->allocs = allocs;
        w->frees = frees;
        API.cuCtxSetCurrent(NULL);
        w->detach_rc = API.cuCtxDetach(a->ctx);
    } else {
        atomic_store(&global_failed, 1);
    }
    return NULL;
}

static void run_shared_phase(CUdevice dev, int ordinal, const char *uuid,
                             const struct config *cfg)
{
    CUcontext ctx = NULL;
    CUmodule mod = NULL;
    CUfunction fn = NULL;
    struct start_gate gate;
    struct shared_args *args;
    int create_rc = -1, load_rc = -1, unload_rc = -1, destroy_rc = -1;
    int i, joined = 0;

    create_rc = API.cuCtxCreate_v2(&ctx, 0, dev);
    if (!create_rc)
        load_rc = load_add_kernel(&mod, &fn);
    if (create_rc || load_rc) {
        record_failuref("shared_setup", "create_rc=%d load_rc=%d",
                        create_rc, load_rc);
        if (mod) unload_rc = API.cuModuleUnload(mod);
        if (ctx) destroy_rc = API.cuCtxDestroy_v2(ctx);
        emit_shared_ctx(ordinal, uuid, cfg->threads, create_rc, load_rc,
                        unload_rc, destroy_rc);
        atomic_store(&global_failed, 1);
        return;
    }
    args = calloc((size_t)cfg->threads, sizeof(*args));
    if (!args) { record_failure("shared_calloc"); goto cleanup_ctx; }
    gate_init(&gate);
    for (i = 0; i < cfg->threads; i++) {
        args[i].dev = dev;
        args[i].index = i;
        args[i].ordinal = ordinal;
        args[i].uuid = uuid;
        args[i].ctx = ctx;
        args[i].fn = fn;
        args[i].elements = cfg->elements;
        args[i].cycles = cfg->cycles;
        args[i].gate = &gate;
        if (pthread_create(&args[i].handle, NULL, shared_thread,
                           &args[i]) != 0) {
            /* Keep the group bounded and record the shortfall; the checker
             * requires every requested worker. */
            record_failuref("shared_pthread_create", "index=%d", i);
            break;
        }
        joined++;
    }
    gate_arm(&gate, joined);
    for (i = 0; i < joined; i++)
        pthread_join(args[i].handle, NULL);
    unload_rc = API.cuModuleUnload(mod);
    destroy_rc = API.cuCtxDestroy_v2(ctx);
    emit_shared_ctx(ordinal, uuid, cfg->threads, create_rc, load_rc,
                    unload_rc, destroy_rc);
    for (i = 0; i < joined; i++)
        emit_thread_worker("shared", &args[i].result);
    if (joined < cfg->threads || unload_rc || destroy_rc)
        atomic_store(&global_failed, 1);
    free(args);
    return;
cleanup_ctx:
    API.cuModuleUnload(mod);
    API.cuCtxDestroy_v2(ctx);
}

/* ---- phase: one thread per card with its own context ------------------- */

struct multicard_args {
    CUdevice dev;
    int index;
    int ordinal;
    const char *uuid;
    unsigned elements;
    int cycles;
    struct start_gate *gate;
    pthread_t handle;
    struct worker_result result;
};

static void *multicard_thread(void *arg)
{
    struct multicard_args *a = arg;
    CUcontext ctx = NULL;
    CUmodule mod = NULL;
    CUfunction fn = NULL;
    uint32_t allocs = 0, frees = 0;
    struct worker_result *w = &a->result;

    memset(w, 0, sizeof(*w));
    w->worker = a->index;
    w->ordinal = a->ordinal;
    strncpy(w->uuid, a->uuid, UUID_LEN - 1);
    w->pid = getpid();
    w->tid = gettid_();
    w->elements = (int)a->elements;
    w->cycles = a->cycles;
    w->attach_rc = w->detach_rc = 0;
    w->create_rc = API.cuCtxCreate_v2(&ctx, 0, a->dev);
    if (!w->create_rc)
        w->load_rc = load_add_kernel(&mod, &fn);
    w->ready_ns = now_ns();
    gate_wait(a->gate);
    if (!w->create_rc && !w->load_rc) {
        w->start_ns = now_ns();
        if (run_cycles(fn, a->elements, a->cycles, ROLE_MULTICARD, 0,
                       (uint32_t)a->index, 0, w->cycle, &allocs, &frees))
            atomic_store(&global_failed, 1);
        w->end_ns = now_ns();
        w->allocs = allocs;
        w->frees = frees;
        w->unload_rc = API.cuModuleUnload(mod);
        w->destroy_rc = API.cuCtxDestroy_v2(ctx);
    } else {
        if (mod) API.cuModuleUnload(mod);
        if (ctx) API.cuCtxDestroy_v2(ctx);
        atomic_store(&global_failed, 1);
    }
    return NULL;
}

static void run_multicard_phase(const struct config *cfg,
                                const CUdevice *devs, const char **uuids)
{
    struct start_gate gate;
    struct multicard_args *args;
    int i, joined = 0;

    args = calloc((size_t)cfg->ngpu, sizeof(*args));
    if (!args) { record_failure("multicard_calloc"); return; }
    gate_init(&gate);
    for (i = 0; i < cfg->ngpu; i++) {
        args[i].dev = devs[i];
        args[i].index = i;
        args[i].ordinal = cfg->ordinals[i];
        args[i].uuid = uuids[i];
        args[i].elements = cfg->elements;
        args[i].cycles = cfg->cycles;
        args[i].gate = &gate;
        if (pthread_create(&args[i].handle, NULL, multicard_thread,
                           &args[i]) != 0) {
            record_failuref("multicard_pthread_create", "ordinal=%d",
                            cfg->ordinals[i]);
            break;
        }
        joined++;
    }
    gate_arm(&gate, joined);
    for (i = 0; i < joined; i++)
        pthread_join(args[i].handle, NULL);
    for (i = 0; i < joined; i++)
        emit_thread_worker("multicard", &args[i].result);
    if (joined < cfg->ngpu)
        atomic_store(&global_failed, 1);
    free(args);
}

/* ---- phase: fork+exec children ---------------------------------------- */

static int any_killed(const struct child_observation *obs, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++)
        if (obs[i].killed)
            return 1;
    return 0;
}

static const char *uuid_for_ordinal(const struct config *cfg,
                                    const char **uuids, int ordinal)
{
    int k;
    for (k = 0; k < cfg->ngpu; k++)
        if (cfg->ordinals[k] == ordinal)
            return uuids[k];
    return uuids[0];
}

/* Parse one key=<integer> token of the internal child pipe protocol. */
static int proto_int(const char *line, const char *key, long long *out)
{
    char pat[64], *p, *end;
    snprintf(pat, sizeof(pat), "%s=", key);
    p = strstr(line, pat);
    if (!p) return -1;
    errno = 0;
    *out = strtoll(p + strlen(pat), &end, 10);
    return errno || end == p + strlen(pat) ? -1 : 0;
}

static int proto_str(const char *line, const char *key, char *out, size_t n)
{
    char pat[64], *p, *end;
    size_t len;
    snprintf(pat, sizeof(pat), "%s=", key);
    p = strstr(line, pat);
    if (!p) return -1;
    p += strlen(pat);
    end = strpbrk(p, " \n");
    len = (end ? (size_t)(end - p) : strlen(p));
    if (len >= n) return -1;
    memcpy(out, p, len);
    out[len] = '\0';
    return 0;
}

/* One comma-separated cycle record:
 * idx,seed,a0,a1,a2,htod,launch,sync,dtoh,f0,f1,f2,mismatches,csum */
static int proto_cycle(const char *tok, struct cycle_result *c)
{
    char buf[160], *save = NULL, *field;
    int vals[14], k = 0, j;

    memset(c, 0, sizeof(*c));
    snprintf(buf, sizeof(buf), "%s", tok);
    for (field = strtok_r(buf, ",", &save); field;
         field = strtok_r(NULL, ",", &save)) {
        char *end;
        long v;
        if (k >= 14) return -1;
        errno = 0;
        v = strtol(field, &end, 10);
        if (errno || *end) return -1;
        vals[k++] = (int)v;
    }
    if (k != 14) return -1;
    c->idx = (uint32_t)vals[0];
    c->seed = (uint32_t)vals[1];
    for (j = 0; j < 3; j++) c->alloc_rc[j] = vals[2 + j];
    c->htod_rc = vals[5]; c->launch_rc = vals[6]; c->sync_rc = vals[7];
    c->dtoh_rc = vals[8];
    for (j = 0; j < 3; j++) c->free_rc[j] = vals[9 + j];
    c->mismatches = (uint32_t)vals[12];
    c->csum = (uint32_t)vals[13];
    return 0;
}

static int parse_child_line(const char *line, struct child_observation *o)
{
    char *copy, *save = NULL, *tok;
    int ncycles = 0;
    long long v;

    if (strncmp(line, "LCYC1 ", 6))
        return -1;
    if (proto_int(line, "rc", &v)) return -1;
    o->create_rc = (int)v;
    if (proto_int(line, "lr", &v)) return -1;
    o->load_rc = (int)v;
    if (proto_int(line, "ur", &v)) return -1;
    o->unload_rc = (int)v;
    if (proto_int(line, "dr", &v)) return -1;
    o->destroy_rc = (int)v;
    if (proto_int(line, "pid", &v)) return -1;
    o->reported_pid = (pid_t)v;
    if (proto_int(line, "ppid", &v)) return -1;
    o->reported_ppid = (pid_t)v;
    if (proto_int(line, "ord", &v)) return -1;
    o->ordinal = (int)v;
    if (proto_int(line, "el", &v)) return -1;
    o->elements = (int)v;
    if (proto_int(line, "cy", &v)) return -1;
    o->cycles = (int)v;
    if (proto_int(line, "al", &v)) return -1;
    o->allocs = (uint32_t)v;
    if (proto_int(line, "fr", &v)) return -1;
    o->frees = (uint32_t)v;
    if (proto_int(line, "mm", &v)) return -1;
    o->mismatches = (uint32_t)v;
    if (proto_int(line, "st", &v)) return -1;
    o->start_ns = v;
    if (proto_int(line, "en", &v)) return -1;
    o->end_ns = v;
    if (proto_str(line, "uuid", o->uuid, sizeof(o->uuid))) return -1;
    if (o->cycles <= 0 || o->cycles > MAX_CYCLES) return -1;
    copy = strdup(line);
    if (!copy) return -1;
    for (tok = strtok_r(copy, " ", &save); tok;
         tok = strtok_r(NULL, " ", &save)) {
        if (!strncmp(tok, "c=", 2)) {
            char *csave = NULL, *ct;
            for (ct = strtok_r(tok + 2, ";", &csave); ct;
                 ct = strtok_r(NULL, ";", &csave)) {
                if (ncycles >= o->cycles) { free(copy); return -1; }
                if (proto_cycle(ct, &o->cycle[ncycles])) {
                    free(copy); return -1;
                }
                /* Identity is implicit in the compact wire protocol: every
                 * child is a single worker (proc = seq+1, worker 0,
                 * round 0); restore it so the emitted JSON carries the
                 * same explicit identity as in-process phases. */
                o->cycle[ncycles].role = ROLE_CHILD;
                o->cycle[ncycles].proc = (uint32_t)o->seq + 1;
                o->cycle[ncycles].worker = 0;
                o->cycle[ncycles].round = 0;
                ncycles++;
            }
        }
    }
    free(copy);
    return ncycles == o->cycles ? 0 : -1;
}

static void run_child_phase(const char *self, const struct config *cfg,
                            const char **uuids)
{
    int gate[2] = { -1, -1 };
    int *out_r = NULL, *out_w = NULL;
    struct child_observation *obs = NULL;
    struct pollfd *pfds = NULL;
    char **lines = NULL;
    size_t *line_len = NULL;
    int n = cfg->children, i, forked = 0;
    int64_t deadline;

    obs = calloc((size_t)n, sizeof(*obs));
    pfds = calloc((size_t)n, sizeof(*pfds));
    lines = calloc((size_t)n, sizeof(*lines));
    line_len = calloc((size_t)n, sizeof(*line_len));
    out_r = calloc((size_t)n, sizeof(*out_r));
    out_w = calloc((size_t)n, sizeof(*out_w));
    if (!obs || !pfds || !lines || !line_len || !out_r || !out_w) {
        record_failure("child_calloc");
        goto out;
    }
    for (i = 0; i < n; i++) {
        out_r[i] = out_w[i] = -1;
        obs[i].seq = i;
        obs[i].exit_code = -1;
        obs[i].ordinal = cfg->ordinals[i % cfg->ngpu];
        snprintf(obs[i].uuid, sizeof(obs[i].uuid), "%s",
                 uuid_for_ordinal(cfg, uuids, obs[i].ordinal));
    }
    if (pipe(gate) < 0) { record_failure("child_gate_pipe"); goto out; }
    for (i = 0; i < n; i++) {
        int pp[2];
        if (pipe(pp) < 0) {
            record_failure("child_pipe");
            break;
        }
        out_r[i] = pp[0];
        out_w[i] = pp[1];
    }
    deadline = now_ms() + cfg->deadline_ms;
    for (i = 0; i < n; i++) {
        char sord[16], sseq[16], sgate[16], sout[16], sel[16], scyc[16];
        char suuid[UUID_LEN];
        pid_t pid;
        int ord = obs[i].ordinal;
        if (out_w[i] < 0) break;
        snprintf(sord, sizeof(sord), "%d", ord);
        snprintf(sseq, sizeof(sseq), "%d", i);
        snprintf(sgate, sizeof(sgate), "%d", gate[0]);
        snprintf(sout, sizeof(sout), "%d", out_w[i]);
        snprintf(sel, sizeof(sel), "%u", cfg->elements);
        snprintf(scyc, sizeof(scyc), "%d", cfg->child_cycles);
        snprintf(suuid, sizeof(suuid), "%s",
                 uuid_for_ordinal(cfg, uuids, ord));
        obs[i].fork_ns = now_ns();
        pid = fork();
        if (pid < 0) {
            record_failure("child_fork");
            obs[i].fork_ns = 0;
            break;
        }
        if (pid == 0) {
            int fd, top;
            /* Post-fork, pre-exec: only async-signal-safe calls below.
             * No CUDA call is ever made in this child; CUDA is initialized
             * afresh by the new program after execv. */
            close(gate[1]);
            for (int j = 0; j < n; j++) {
                close(out_r[j]);
                if (j != i)
                    close(out_w[j]);
            }
            top = (int)sysconf(_SC_OPEN_MAX);
            if (top < 0 || top > 1024)
                top = 1024;
            for (fd = 3; fd < top; fd++)
                if (fd != gate[0] && fd != out_w[i])
                    close(fd);
            execl(self, self, "--child", "--ordinal", sord, "--seq", sseq,
                  "--gate-fd", sgate, "--out-fd", sout, "--elements", sel,
                  "--cycles", scyc, "--uuid", suuid, (char *)NULL);
            {
                /* Report exec failure through the result pipe; write() and
                 * _exit() are async-signal-safe. */
                char digits[16], buf[48];
                int e = errno, z = 0, k = 0, a, b2;
                do { digits[z++] = (char)('0' + e % 10); e /= 10; }
                while (e);
                for (a = 0, b2 = z - 1; a < b2; a++, b2--) {
                    char t = digits[a]; digits[a] = digits[b2]; digits[b2] = t;
                }
                memcpy(buf + k, "LERR1 e=", 8); k += 8;
                memcpy(buf + k, digits, (size_t)z); k += z;
                buf[k++] = '\n';
                (void)!write(out_w[i], buf, (size_t)k);
                _exit(127);
            }
        }
        forked++;
        obs[i].child_pid = pid;
        close(out_w[i]);
        out_w[i] = -1;
    }
    /* Stamp the release instant before writing: a child's gate-read
     * (and hence start_ns) happens-after the write, so start_ns >= this
     * stamp on every CPU without relying on scheduling order. One gate
     * byte per launched child, then both gate ends close here. */
    {
        int64_t release = now_ns();
        int j;
        for (j = 0; j < forked; j++)
            (void)!write(gate[1], "g", 1);
        for (i = 0; i < forked; i++)
            obs[i].release_ns = release;
    }
    close(gate[1]); gate[1] = -1;
    close(gate[0]); gate[0] = -1;

    for (i = 0; i < forked; i++) {
        fcntl(out_r[i], F_SETFL, O_NONBLOCK);
        pfds[i].fd = out_r[i];
        pfds[i].events = POLLIN;
    }
    /* Drain until every child closed its pipe end or the bounded deadline
     * expires.  Records are sub-PIPE_BUF; buffering here is defensive. */
    for (;;) {
        int open_fds = 0, ready, i2;
        int wait_ms;
        for (i = 0; i < forked; i++)
            if (pfds[i].fd >= 0)
                open_fds++;
        if (!open_fds || now_ms() >= deadline)
            break;
        wait_ms = (int)(deadline - now_ms());
        if (wait_ms > 1000) wait_ms = 1000;
        ready = poll(pfds, (nfds_t)forked, wait_ms);
        if (ready < 0) {
            if (errno == EINTR) continue;
            record_failure("child_poll");
            break;
        }
        for (i2 = 0; i2 < forked; i2++) {
            ssize_t r;
            char chunk[1024];
            if (pfds[i2].fd < 0 ||
                !(pfds[i2].revents & (POLLIN | POLLHUP | POLLERR)))
                continue;
            while ((r = read(pfds[i2].fd, chunk, sizeof(chunk))) > 0) {
                size_t old = line_len[i2];
                char *p = realloc(lines[i2], old + (size_t)r + 1);
                if (!p) { record_failure("child_line_realloc"); break; }
                lines[i2] = p;
                memcpy(p + old, chunk, (size_t)r);
                line_len[i2] = old + (size_t)r;
                p[line_len[i2]] = '\0';
                if (memchr(chunk, '\n', (size_t)r) && !obs[i2].firstbyte_ns)
                    obs[i2].firstbyte_ns = now_ns();
            }
            if (r == 0)
                pfds[i2].fd = -1;
            else if (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
                pfds[i2].fd = -1;
            pfds[i2].revents = 0;
        }
    }
    for (i = 0; i < forked; i++) {
        if (!lines[i])
            continue;
        if (!strncmp(lines[i], "LCYC1 ", 6)) {
            if (parse_child_line(lines[i], &obs[i]) == 0)
                obs[i].received = 1;
            else
                record_failuref("child_parse", "seq=%d", i);
        } else if (!strncmp(lines[i], "LERR1 ", 6)) {
            long long e = 0;
            if (proto_int(lines[i], "e", &e) == 0)
                obs[i].exec_errno = (int)e;
            record_failuref("child_exec", "seq=%d errno=%d", i,
                            obs[i].exec_errno);
        } else {
            record_failuref("child_garbage", "seq=%d", i);
        }
    }
    for (i = 0; i < forked; i++) {
        int status = 0;
        pid_t r = 0;
        while (now_ms() < deadline) {
            pid_t q = waitpid(obs[i].child_pid, &status, WNOHANG);
            if (q == obs[i].child_pid) { r = q; break; }
            if (q < 0 && errno != EINTR) break;
            usleep(5000);
        }
        if (r != obs[i].child_pid) {
            pid_t q;
            /* Overdue own child: signal only this exact pid, never a
             * process group or anything else. */
            kill(obs[i].child_pid, SIGKILL);
            obs[i].killed = 1;
            do q = waitpid(obs[i].child_pid, &status, 0);
            while (q < 0 && errno == EINTR);
            if (q == obs[i].child_pid) r = q;
        }
        obs[i].wait_ns = now_ns();
        if (r == obs[i].child_pid) {
            if (WIFEXITED(status)) obs[i].exit_code = WEXITSTATUS(status);
            else if (WIFSIGNALED(status)) obs[i].exit_signal = WTERMSIG(status);
        }
    }
    for (i = 0; i < forked; i++)
        emit_child_worker(&obs[i]);
    if (forked < n || any_killed(obs, (size_t)forked))
        atomic_store(&global_failed, 1);
out:
    for (i = 0; i < n; i++) {
        if (out_r && out_r[i] >= 0) close(out_r[i]);
        if (out_w && out_w[i] >= 0) close(out_w[i]);
    }
    if (gate[0] >= 0) close(gate[0]);
    if (gate[1] >= 0) close(gate[1]);
    free(obs); free(pfds); free(lines); free(line_len);
    free(out_r); free(out_w);
}

/* Bounded one-line child report formatter.  Fails closed with -1 when the
 * report does not fit; it never truncates and never overruns the buffer.
 * The worst-case size (INT_MIN rc values, UINT32_MAX counters, MAX_CYCLES
 * records) is exercised by tests/cuda_lifecycle_host_fixture.c and must
 * stay below CHILD_LINE_MAX. */
struct child_line_input {
    int seq;
    int ordinal;
    int elements;
    int cycles;
    pid_t pid;
    pid_t ppid;
    uint32_t allocs;
    uint32_t frees;
    uint32_t mismatches;
    int create_rc;
    int load_rc;
    int unload_rc;
    int destroy_rc;
    int64_t start_ns;
    int64_t end_ns;
    const char *uuid;
    const struct cycle_result *cyc;
};

static int format_child_line(char *line, size_t cap,
                             const struct child_line_input *in)
{
    size_t z = 0;
    int w, i;

    if (!line || cap == 0)
        return -1;
    w = snprintf(line, cap,
        "LCYC1 seq=%d ord=%d pid=%ld ppid=%ld el=%d cy=%d al=%u fr=%u "
        "mm=%u rc=%d lr=%d ur=%d dr=%d st=%lld en=%lld uuid=%s",
        in->seq, in->ordinal, (long)in->pid, (long)in->ppid, in->elements,
        in->cycles, in->allocs, in->frees, in->mismatches, in->create_rc,
        in->load_rc, in->unload_rc, in->destroy_rc,
        (long long)in->start_ns, (long long)in->end_ns, in->uuid);
    if (w < 0 || (size_t)w + 1 > cap)
        return -1;
    z = (size_t)w;
    for (i = 0; i < in->cycles; i++) {
        const struct cycle_result *c = &in->cyc[i];
        size_t remain = cap - z;
        w = snprintf(line + z, remain,
                     "%s%u,%u,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%u,%u",
                     i == 0 ? " c=" : ";", c->idx, c->seed, c->alloc_rc[0],
                     c->alloc_rc[1], c->alloc_rc[2], c->htod_rc,
                     c->launch_rc, c->sync_rc, c->dtoh_rc, c->free_rc[0],
                     c->free_rc[1], c->free_rc[2], c->mismatches, c->csum);
        if (w < 0 || (size_t)w + 1 > remain)
            return -1;
        z += (size_t)w;
    }
    {
        size_t remain = cap - z;
        w = snprintf(line + z, remain, "\n");
        if (w < 0 || (size_t)w + 1 > remain)
            return -1;
    }
    return 0;
}

/* ---- child mode: fresh CUDA after exec --------------------------------- */

static int child_main(int argc, char **argv)
{
    int ordinal = -1, seq = -1, gate_fd = -1, out_fd = -1, cycles = -1;
    unsigned elements = 0;
    char expect_uuid[UUID_LEN] = "";
    CUdevice dev;
    CUcontext ctx = NULL;
    CUmodule mod = NULL;
    CUfunction fn = NULL;
    struct cycle_result cyc[MAX_CYCLES];
    uint32_t allocs = 0, frees = 0, mismatches = 0;
    int create_rc = -1, load_rc = -1, unload_rc = -1, destroy_rc = -1;
    int rc, i, bad = 0, gate_read;
    int64_t start_ns = 0, end_ns;
    char line[CHILD_LINE_MAX];
    char gate_byte;
    unsigned char uuid_bytes[16] = {0};
    char uuid[UUID_LEN];

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--ordinal") && i + 1 < argc)
            ordinal = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--seq") && i + 1 < argc)
            seq = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--gate-fd") && i + 1 < argc)
            gate_fd = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--out-fd") && i + 1 < argc)
            out_fd = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--elements") && i + 1 < argc)
            elements = (unsigned)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--cycles") && i + 1 < argc)
            cycles = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--uuid") && i + 1 < argc)
            snprintf(expect_uuid, sizeof(expect_uuid), "%s", argv[++i]);
    }
    if (ordinal < 0 || seq < 0 || gate_fd < 0 || out_fd < 0 ||
        cycles <= 0 || cycles > MAX_CYCLES || elements == 0)
        return 2;
    do { gate_read = read(gate_fd, &gate_byte, 1); }
    while (gate_read < 0 && errno == EINTR);
    if (gate_read != 1)
        return 3;

    rc = API.cuInit(0);
    if (rc) { fprintf(stderr, "child cuInit rc=%d\n", rc); return 4; }
    rc = API.cuDeviceGet(&dev, ordinal);
    if (rc) { fprintf(stderr, "child cuDeviceGet rc=%d\n", rc); return 5; }
    if (API.cuDeviceGetUuid(uuid_bytes, dev)) {
        fprintf(stderr, "child cuDeviceGetUuid failed\n");
        return 5;
    }
    uuid_format(uuid_bytes, uuid);
    if (expect_uuid[0] && strcmp(uuid, expect_uuid)) {
        fprintf(stderr, "child uuid mismatch %s != %s\n", uuid, expect_uuid);
        return 6;
    }
    create_rc = API.cuCtxCreate_v2(&ctx, 0, dev);
    if (create_rc) { fprintf(stderr, "child create rc=%d\n", create_rc); return 7; }
    load_rc = load_add_kernel(&mod, &fn);
    if (load_rc) { API.cuCtxDestroy_v2(ctx); return 8; }
    start_ns = now_ns();
    bad = run_cycles(fn, elements, cycles, ROLE_CHILD, (uint32_t)(seq + 1),
                     0, 0, cyc, &allocs, &frees);
    end_ns = now_ns();
    if (bad < 0) {
        /* Host-side failure (e.g. malloc) before cycles ran: the cycle
         * array never filled, so fail closed instead of serializing stack
         * contents.  Driver resources are released first. */
        API.cuModuleUnload(mod);
        API.cuCtxDestroy_v2(ctx);
        {
            const char msg[] = "LERR1 e=-1 h=1\n";
            size_t done = 0;
            while (done < sizeof(msg) - 1) {
                ssize_t w = write(out_fd, msg + done,
                                  sizeof(msg) - 1 - done);
                if (w < 0 && errno == EINTR) continue;
                if (w < 0) break;
                done += (size_t)w;
            }
        }
        return 11;
    }
    for (i = 0; i < cycles; i++)
        mismatches += cyc[i].mismatches;
    unload_rc = API.cuModuleUnload(mod);
    destroy_rc = API.cuCtxDestroy_v2(ctx);
    if (unload_rc || destroy_rc)
        bad = 1;

    {
        struct child_line_input in;
        memset(&in, 0, sizeof(in));
        in.seq = seq; in.ordinal = ordinal; in.pid = getpid();
        in.ppid = getppid(); in.elements = (int)elements; in.cycles = cycles;
        in.allocs = allocs; in.frees = frees; in.mismatches = mismatches;
        in.create_rc = create_rc; in.load_rc = load_rc;
        in.unload_rc = unload_rc; in.destroy_rc = destroy_rc;
        in.start_ns = start_ns; in.end_ns = end_ns; in.uuid = uuid;
        in.cyc = cyc;
        if (format_child_line(line, sizeof(line), &in) != 0) {
            /* mod/ctx were already released above.  Fail closed without
             * emitting a truncated report. */
            fprintf(stderr, "child report too large\n");
            return 9;
        }
    }
    {
        size_t need = strlen(line), done = 0;
        while (done < need) {
            ssize_t w = write(out_fd, line + done, need - done);
            if (w < 0 && errno == EINTR) continue;
            if (w < 0) return 10;
            done += (size_t)w;
        }
    }
    return bad ? 1 : 0;
}

/* ---- parent driver ----------------------------------------------------- */

static int load_api(void)
{
    void *lib = dlopen("libcuda.so.1", RTLD_NOW | RTLD_LOCAL);
#define NEED(x) do { *(void **)&API.x = dlsym(lib, #x); \
        if (!API.x) { fprintf(stderr, "missing %s\n", #x); \
                      dlclose(lib); return -1; } } while (0)
    if (!lib) {
        fprintf(stderr, "%s\n", dlerror());
        return -1;
    }
    NEED(cuInit); NEED(cuDeviceGetCount); NEED(cuDeviceGet);
    NEED(cuDeviceGetName); NEED(cuDeviceGetUuid); NEED(cuDeviceTotalMem_v2);
    NEED(cuCtxCreate_v2); NEED(cuCtxDestroy_v2); NEED(cuCtxSetCurrent);
    NEED(cuCtxAttach); NEED(cuCtxDetach); NEED(cuCtxSynchronize);
    NEED(cuMemAlloc_v2); NEED(cuMemFree_v2); NEED(cuMemcpyHtoD_v2);
    NEED(cuMemcpyDtoH_v2); NEED(cuMemGetInfo_v2); NEED(cuModuleLoadData);
    NEED(cuModuleUnload); NEED(cuModuleGetFunction); NEED(cuLaunchKernel);
    return 0;
#undef NEED
}

static int parse_ordinals(const char *s, struct config *cfg, int count)
{
    char *copy, *tok, *save = NULL;
    cfg->ngpu = 0;
    copy = strdup(s);
    if (!copy) return -1;
    for (tok = strtok_r(copy, ",", &save); tok;
         tok = strtok_r(NULL, ",", &save)) {
        char *end;
        long v = strtol(tok, &end, 10);
        int k, duplicate = 0;
        if (*end || v < 0 || v >= count || cfg->ngpu >= MAX_GPUS) {
            free(copy); return -1;
        }
        for (k = 0; k < cfg->ngpu; k++)
            if (cfg->ordinals[k] == (int)v) duplicate = 1;
        if (duplicate) { free(copy); return -1; }
        cfg->ordinals[cfg->ngpu++] = (int)v;
    }
    free(copy);
    return cfg->ngpu ? 0 : -1;
}

static void usage(const char *prog)
{
    fprintf(stderr,
        "usage: %s [--cycles 1..%d] [--ctx-rounds 1..%d] [--threads 4..%d]\n"
        "          [--children 1..%d] [--elements 4096..1048576]\n"
        "          [--deadline-ms 500..60000] [--gpus 0,2,..]\n"
        "       %s --child --ordinal N --seq N --gate-fd FD --out-fd FD\n"
        "          --elements N --cycles N [--uuid GPU-...]\n",
        prog, MAX_CYCLES, MAX_ROUNDS, DEFAULT_THREADS, MAX_CHILDREN, prog);
}

int main(int argc, char **argv)
{
    struct config cfg = default_config;
    int child_mode = 0;
    const char *gpus_arg = NULL;
    char selfbuf[4096] = "";
    const char *self = argv[0];
    int i, count = 0, init_rc;
    CUdevice *devs = NULL;
    char (*names)[NAME_LEN] = NULL;
    char (*uuids)[UUID_LEN] = NULL;
    const char **uuidp = NULL;
    int64_t begin_ns = 0, end_ns;

    setvbuf(stdout, NULL, _IOLBF, 0);
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--child")) child_mode = 1;
        else if (!strcmp(argv[i], "--cycles") && i + 1 < argc)
            cfg.cycles = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--ctx-rounds") && i + 1 < argc)
            cfg.ctx_rounds = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--threads") && i + 1 < argc)
            cfg.threads = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--children") && i + 1 < argc)
            cfg.children = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--elements") && i + 1 < argc)
            cfg.elements = (unsigned)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--deadline-ms") && i + 1 < argc)
            cfg.deadline_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--gpus") && i + 1 < argc)
            gpus_arg = argv[++i];
        else if (child_mode && !strcmp(argv[i], "--ordinal") && i + 1 < argc)
            i++;
        else if (child_mode && !strcmp(argv[i], "--seq") && i + 1 < argc)
            i++;
        else if (child_mode && !strcmp(argv[i], "--gate-fd") && i + 1 < argc)
            i++;
        else if (child_mode && !strcmp(argv[i], "--out-fd") && i + 1 < argc)
            i++;
        else if (child_mode && !strcmp(argv[i], "--uuid") && i + 1 < argc)
            i++;
        else { usage(argv[0]); return 2; }
    }
    if (load_api() < 0)
        return 2;
    if (child_mode)
        return child_main(argc, argv);
    if (cfg.cycles < 1 || cfg.cycles > MAX_CYCLES ||
        cfg.ctx_rounds < 1 || cfg.ctx_rounds > MAX_ROUNDS ||
        cfg.threads < DEFAULT_THREADS || cfg.threads > MAX_THREADS ||
        (cfg.children != -1 &&
         (cfg.children < 1 || cfg.children > MAX_CHILDREN)) ||
        cfg.elements < 4096 || cfg.elements > (1u << 20) ||
        cfg.deadline_ms < 500 || cfg.deadline_ms > 60000) {
        usage(argv[0]);
        return 2;
    }
    /* execv needs a real path even if the binary was found through $PATH. */
    if (readlink("/proc/self/exe", selfbuf, sizeof(selfbuf) - 1) > 0)
        self = selfbuf;

    init_rc = API.cuInit(0);
    if (init_rc) {
        /* Resolve the documented default before the meta record so the
         * emitted configuration is always the effective one. */
        cfg.children = DEFAULT_THREADS;
        emit_meta(&cfg, init_rc);
        record_failuref("cuinit", "rc=%d", init_rc);
        goto finish_failed;
    }
    if (API.cuDeviceGetCount(&count) || count <= 0) {
        cfg.children = DEFAULT_THREADS;
        emit_meta(&cfg, init_rc);
        record_failure("device_count");
        goto finish_failed;
    }
    if (count > MAX_GPUS) count = MAX_GPUS;
    if (gpus_arg) {
        if (parse_ordinals(gpus_arg, &cfg, count) < 0) {
            usage(argv[0]); return 2;
        }
    } else {
        for (i = 0; i < count; i++)
            cfg.ordinals[i] = i;
        cfg.ngpu = count;
    }
    /* Resolve the child count, then validate it against the hard bounds
     * for the resolved GPU set: one fresh-CUDA child per card by default,
     * never fewer than four; clamp/never silently exceed MAX_CHILDREN. */
    if (cfg.children == -1)
        cfg.children = cfg.ngpu > DEFAULT_THREADS ? cfg.ngpu
                                                  : DEFAULT_THREADS;
    if (cfg.children < 1 || cfg.children > MAX_CHILDREN) {
        fprintf(stderr, "resolved child count %d outside 1..%d\n",
                cfg.children, MAX_CHILDREN);
        return 2;
    }
    emit_meta(&cfg, init_rc);

    devs = calloc((size_t)cfg.ngpu, sizeof(*devs));
    names = calloc((size_t)cfg.ngpu, sizeof(*names));
    uuids = calloc((size_t)cfg.ngpu, sizeof(*uuids));
    uuidp = calloc((size_t)cfg.ngpu, sizeof(*uuidp));
    if (!devs || !names || !uuids || !uuidp) {
        record_failure("parent_calloc");
        goto finish_failed;
    }
    for (i = 0; i < cfg.ngpu; i++) {
        int ord = cfg.ordinals[i];
        /* Zero-initialized: if cuDeviceGetUuid fails the UUID formatter
         * must never read uninitialized bytes.  A failed query yields the
         * all-zero GPU-0000.. marker with uuid_valid=false, which the
         * checker rejects; no fabricated UUID is presented as real. */
        unsigned char bytes[16] = {0};
        size_t total = 0;
        int bad = 0, uuid_ok = 1, total_ok = 1;
        if (API.cuDeviceGet(&devs[i], ord)) bad = 1;
        if (API.cuDeviceGetName(names[i], NAME_LEN, devs[i])) bad = 1;
        if (API.cuDeviceGetUuid(bytes, devs[i])) { bad = 1; uuid_ok = 0; }
        if (API.cuDeviceTotalMem_v2(&total, devs[i])) { bad = 1; total_ok = 0; }
        if (bad) {
            record_failuref("inventory_query", "ordinal=%d", ord);
            snprintf(names[i], NAME_LEN, "invalid");
        }
        uuid_format(bytes, uuids[i]);
        uuidp[i] = uuids[i];
        emit_inventory(ord, uuids[i], names[i],
                       total_ok ? (uint64_t)total : 0, uuid_ok);
    }

    begin_ns = now_ns();
    for (i = 0; i < cfg.ngpu; i++)
        run_solo_phase(devs[i], i, cfg.ordinals[i], uuids[i], &cfg);
    run_shared_phase(devs[0], cfg.ordinals[0], uuids[0], &cfg);
    run_multicard_phase(&cfg, devs, uuidp);
    run_child_phase(self, &cfg, uuidp);
    end_ns = now_ns();

    {
        struct jb b;
        jb_init(&b, 512);
        jb_printf(&b, "{\"type\":\"summary\",\"schema\":1,\"status\":\"%s\","
                      "\"device_count\":%d,\"cycles\":%d,\"child_cycles\":%d,"
                      "\"ctx_rounds\":%d,\"threads\":%d,\"children\":%d,"
                      "\"elements\":%u,\"start_ns\":%lld,\"end_ns\":%lld,"
                      "\"pid\":%ld}",
                  atomic_load(&global_failed) ? "fail" : "pass", cfg.ngpu,
                  cfg.cycles, cfg.child_cycles, cfg.ctx_rounds, cfg.threads,
                  cfg.children, cfg.elements, (long long)begin_ns,
                  (long long)end_ns, (long)getpid());
        emit(&b);
    }
    free(devs); free(names); free(uuids); free(uuidp);
    return atomic_load(&global_failed) ? 1 : 0;

finish_failed:
    {
        struct jb b;
        jb_init(&b, 256);
        jb_printf(&b, "{\"type\":\"summary\",\"schema\":1,\"status\":\"fail\","
                      "\"device_count\":0,\"cycles\":%d,\"child_cycles\":%d,"
                      "\"ctx_rounds\":%d,\"threads\":%d,\"children\":0,"
                      "\"elements\":%u,\"pid\":%ld}",
                  cfg.cycles, cfg.child_cycles, cfg.ctx_rounds, cfg.threads,
                  cfg.elements, (long)getpid());
        emit(&b);
    }
    free(devs); free(names); free(uuids); free(uuidp);
    return 1;
}
