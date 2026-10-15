/*
 * Actual CUDA driver-API lifecycle probe (mapping-owner-v14 follow-up).
 *
 * dlopen("libcuda.so.1") only; declared driver ABI, no CUDA toolkit.
 * Covers, on every enumerated GPU:
 *   - solo repeated alloc/HtoD/kernel/DtoH-validate/free (free+realloc
 *     pattern) with context destruction cleanup;
 *   - concurrent INDEPENDENT contexts, one per card, in 4 pthreads;
 *   - 4 independent fresh-exec child processes (fork before the child
 *     touches CUDA; the child reinitializes in a new program image);
 *   - peer memory transfer for every ordered pair cuDeviceCanAccessPeer
 *     reports accessible (HtoD -> cuMemcpyDtoD across contexts -> DtoH
 *     byte check); non-accessible pairs are recorded honestly as
 *     unsupported, never skipped as pass and never forced;
 *   - inventory enumeration validated against the requested card set.
 *
 * No global GPU reset, no other client's handles.  All workloads are
 * bounded and finite; this is correctness, not performance acceptance.
 * JSONL on stdout: raw CUDA status, bytes checked, identities and
 * intervals.  Strict independent checker: scripts/cuda_lifecycle_checks.py
 *
 * Driver ABI references: CUDA 12.4 archive docs (driver 550.120), see
 * docs/cuda-12.4-peer-api.txt; pinned PTX .version 7.0 sm_70.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
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
    CUresult (*cuDeviceCanAccessPeer)(int *, CUdevice, CUdevice);
    CUresult (*cuCtxCreate_v2)(CUcontext *, unsigned, CUdevice);
    CUresult (*cuCtxDestroy_v2)(CUcontext);
    CUresult (*cuCtxSetCurrent)(CUcontext);
    CUresult (*cuCtxEnablePeerAccess)(CUcontext, unsigned);
    CUresult (*cuMemAlloc_v2)(CUdeviceptr *, size_t);
    CUresult (*cuMemFree_v2)(CUdeviceptr);
    CUresult (*cuMemGetInfo_v2)(size_t *, size_t *);
    CUresult (*cuMemcpyHtoD_v2)(CUdeviceptr, const void *, size_t);
    CUresult (*cuMemcpyDtoH_v2)(void *, CUdeviceptr, size_t);
    CUresult (*cuMemcpyDtoD_v2)(CUdeviceptr, CUdeviceptr, size_t);
    CUresult (*cuModuleLoadData)(CUmodule *, const void *);
    CUresult (*cuModuleUnload)(CUmodule);
    CUresult (*cuModuleGetFunction)(CUfunction *, CUmodule, const char *);
    CUresult (*cuLaunchKernel)(CUfunction, unsigned, unsigned, unsigned,
                               unsigned, unsigned, unsigned, unsigned,
                               void *, void **, void **);
    CUresult (*cuCtxSynchronize)(void);
};

/* Byte-identical to the proven kernel in
 * gpu-completion-owner-v1/repo/src/cuda_probe.c (passing 4-A100 native
 * and internal Docker).  Do NOT reformat: the emitter fixture and the
 * docs/proven_add_ptx.txt baseline enforce byte identity.  The old v2
 * string omitted ".reg .f32 f<4>" and the .f32 store type, which made
 * cuModuleLoadData fail with rc 218 on the real driver. */
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

#define DEFAULT_ITERATIONS 8          /* solo alloc/compute/free rounds */
#define DEFAULT_THREAD_CYCLES 4
#define DEFAULT_CHILD_CYCLES 2
#define DEFAULT_THREADS 4
#define DEFAULT_CHILDREN 4
#define DEFAULT_ELEMENTS (1u << 18)   /* 262144 floats = 1 MiB */
#define DEFAULT_DEADLINE_MS 30000
#define MAX_ITEMS 32
#define MAX_THREADS 32
#define MAX_CHILDREN 32
#define MAX_GPUS 32
#define NAMEBUFLEN 256
#define UUID_LEN 41
#define CHILD_LINE_MAX 7240
#define CHILD_CYCLE_TOKEN_MAX 216
#define CHILD_CYCLE_FIELDS 16
_Static_assert(DEFAULT_ELEMENTS * sizeof(float) == (1u << 20),
               "default workload must be exactly 1 MiB");

#define ROLE_SOLO 1u
#define ROLE_THREAD 2u
#define ROLE_CHILD 3u

struct config {
    int cycles;             /* solo iterations */
    int child_cycles;
    int thread_cycles;
    int ctx_rounds;
    int threads;
    int children;
    unsigned elements;
    int deadline_ms;
    int ngpu;
    int ordinals[MAX_ITEMS];
};

struct cycle_result {
    uint32_t idx, seed, role, proc, worker, round;
    int alloc_rc[3], htod_rc, launch_rc, sync_rc, dtoh_rc, free_rc[3];
    uint32_t mismatches, csum;
    int64_t start_ns, end_ns;
};

struct worker_result {
    int worker, ordinal;
    char uuid[UUID_LEN];
    pid_t pid, tid;
    int elements, cycles;
    int completed_cycles;
    uint32_t allocs, frees, bytes_checked, mismatches;
    int create_rc, load_rc, unload_rc, destroy_rc;
    int64_t ready_ns, start_ns, end_ns;
    struct cycle_result cycle[MAX_ITEMS];
};

struct child_observation {
    int seq, ordinal;
    pid_t child_pid, reported_pid, reported_ppid;
    char uuid[UUID_LEN];
    int received, exec_errno, exit_code, exit_signal, killed;
    int elements, cycles, completed_cycles;
    uint32_t allocs, frees, mismatches, bytes_checked;
    int create_rc, load_rc, unload_rc, destroy_rc;
    int64_t start_ns, end_ns, fork_ns, ready_ns, release_ns,
          firstbyte_ns, wait_ns;
    struct cycle_result cycle[MAX_ITEMS];
};

struct jb { char *p; size_t n, cap; int oom; };
struct start_gate { pthread_mutex_t mu; pthread_cond_t cv;
                   int total, waiting, released; };
struct cuda_api API;
static atomic_int global_failed;

/* ---- tiny JSON line buffer ---- */
static void jb_init(struct jb *b, size_t cap) {
    b->p=malloc(cap); b->n=0; b->cap=cap; b->oom=(b->p==NULL); }
static void jb_put(struct jb *b, const char *s) {
    size_t l=strlen(s);
    if (b->n+l>=b->cap) {
        size_t nc=b->cap*2;char*p=realloc(b->p,nc);
        if(!p){b->oom=1;return;}
        b->p=p;b->cap=nc;
    }
    memcpy(b->p+b->n,s,l); b->n+=l;
}
static void jb_printf(struct jb *b, const char *fmt, ...) {
    va_list ap; va_start(ap,fmt);
    char tmp[1024]; vsnprintf(tmp,sizeof(tmp),fmt,ap); va_end(ap);
    jb_put(b,tmp);
}
static void emit(struct jb *b) { pthread_mutex_t *lk=NULL; (void)lk;
    if (!b->oom) { fwrite(b->p,1,b->n,stdout); putchar('\n'); fflush(stdout);} free(b->p); }

static void record_failure(const char *stage) {
    struct jb b; jb_init(&b,160);
    jb_printf(&b,"{\"type\":\"error\",\"stage\":\"%s\",\"errno\":%d}",stage,errno);
    emit(&b); atomic_store(&global_failed,1);
}
static void record_failuref(const char *stage, const char *fmt, ...) {
    char msg[160]; va_list ap; va_start(ap,fmt); vsnprintf(msg,sizeof(msg),fmt,ap); va_end(ap);
    struct jb b; jb_init(&b,256);
    jb_printf(&b,"{\"type\":\"error\",\"stage\":\"%s\",\"message\":\"%s\"}",stage,msg);
    emit(&b); atomic_store(&global_failed,1);
}

static int64_t now_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);
    return (int64_t)t.tv_sec*1000000000ll+t.tv_nsec;}
static int64_t now_ms(void){return now_ns()/1000000;}
static pid_t gettid_(void){return (pid_t)syscall(SYS_gettid);}

static void write_pipe_all(int fd,const void*buf,size_t n){size_t d=0;
    const char*p=buf;while(d<n){ssize_t w=write(fd,p+d,n-d);if(w<0&&errno==EINTR)continue;if(w<0)return;d+=(size_t)w;}}
static int write_pipe_byte(int fd, unsigned char byte) {
    for (;;) {
        ssize_t w = write(fd, &byte, 1);
        if (w == 1) return 0;
        if (w < 0 && errno == EINTR) continue;
        return -1;
    }
}

static void uuid_format(char *out,const unsigned char bytes[16]){
    snprintf((char*)out,UUID_LEN,"GPU-%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        bytes[0],bytes[1],bytes[2],bytes[3],bytes[4],bytes[5],bytes[6],bytes[7],
        bytes[8],bytes[9],bytes[10],bytes[11],bytes[12],bytes[13],bytes[14],bytes[15]);}
static void json_string(struct jb *b,const char *s){jb_printf(b,"\"");unsigned char c;
    while((c=(unsigned char)*s++)){if(c=='"'||c=='\\')jb_printf(b,"\\");jb_printf(b,"%c",c);}jb_printf(b,"\"");}

/* deterministic seed namespace: role/proc/worker/round/cycle */
static uint32_t cycle_seed(uint32_t role,uint32_t proc,uint32_t worker,
                           uint32_t round,uint32_t cycle){
    uint32_t h=0x9e3779b9u;
    uint32_t add=role*0x85ebca77u+proc*0xc2b2ae3du+worker*0x27d4eb2fu+
                 round*0x165667b1u+cycle*0xd1b54a31u; h+=add;
    h^=h>>16;h*=0x7feb352du;h^=h>>15;h*=0x846ca68bu;h^=h>>16;
    return h?h:1u;
}
static float pattern_value(uint32_t i,uint32_t seed,int side){
    if(side==0)return (float)((i*7u+seed*13u)%1009u);
    return (float)((i*11u+seed*17u)%907u+1u);
}

/* Exact validation BEFORE any float->unsigned conversion (NaN safe). */
static int validate_outputs(const float *ha,const float *hb,const float *hc,
                            unsigned elements,uint32_t *mismatches,
                            uint32_t *csum){
    uint32_t m=0,s=0;unsigned j;
    for(j=0;j<elements;j++){
        float got=hc[j],want=ha[j]+hb[j];
        if(!isfinite(got)||got<0.f||got>2000.f||got!=want){m++;continue;}
        s+=(uint32_t)(unsigned)got;
    }
    *mismatches=m;*csum=s;return m?1:0;
}

static int load_api(void){
    void *lib=dlopen("libcuda.so.1",RTLD_NOW|RTLD_LOCAL);
    if(!lib){fprintf(stderr,"%s\n",dlerror());return -1;}
#define NEED(x) do{*(void**)&API.x=dlsym(lib,#x);if(!API.x){fprintf(stderr,"missing %s\n",#x);return -1;}}while(0)
    NEED(cuInit);NEED(cuDeviceGetCount);NEED(cuDeviceGet);NEED(cuDeviceGetName);
    NEED(cuDeviceGetUuid);NEED(cuDeviceTotalMem_v2);NEED(cuDeviceCanAccessPeer);
    NEED(cuCtxCreate_v2);NEED(cuCtxDestroy_v2);NEED(cuCtxSetCurrent);
    NEED(cuCtxEnablePeerAccess);
    NEED(cuMemAlloc_v2);NEED(cuMemFree_v2);NEED(cuMemGetInfo_v2);
    NEED(cuMemcpyHtoD_v2);NEED(cuMemcpyDtoH_v2);NEED(cuMemcpyDtoD_v2);
    NEED(cuModuleLoadData);NEED(cuModuleUnload);NEED(cuModuleGetFunction);
    NEED(cuLaunchKernel);NEED(cuCtxSynchronize);
#undef NEED
    return 0;
}
static int load_add_kernel(CUmodule *mod, CUfunction *fn) {
    int rc = API.cuModuleLoadData(mod, ptx);
    if (rc) return rc;
    rc = API.cuModuleGetFunction(fn, *mod, "add");
    if (rc) { API.cuModuleUnload(*mod); }
    return rc;
}

static int run_cycles(CUfunction fn, unsigned elements, int n, uint32_t role,
                      uint32_t proc, uint32_t worker, uint32_t round,
                      struct cycle_result *out, uint32_t *allocs,
                      uint32_t *frees) {
    size_t bytes=(size_t)elements*sizeof(float);
    float *ha=malloc(bytes),*hb=malloc(bytes),*hc=malloc(bytes);
    int bad=0,i;
    if(!ha||!hb||!hc){free(ha);free(hb);free(hc);return -1;}
    for(i=0;i<n;i++){
        CUdeviceptr da=0,db=0,dc=0;unsigned j;uint32_t seed;
        void *args[4];
        struct cycle_result *c = &out[i];
        seed=cycle_seed(role,proc,worker,round,(uint32_t)i);
        for(j=0;j<elements;j++){ha[j]=pattern_value(j,seed,0);hb[j]=pattern_value(j,seed,1);}
        memset(c,0,sizeof(*c));
        c->idx=(uint32_t)i;c->seed=seed;c->role=role;c->proc=proc;
        c->worker=worker;c->round=round;
        c->start_ns=now_ns();
        c->alloc_rc[0]=API.cuMemAlloc_v2(&da,bytes);
        if(da)c->alloc_rc[1]=API.cuMemAlloc_v2(&db,bytes);else c->alloc_rc[1]=-1;
        if(db)c->alloc_rc[2]=API.cuMemAlloc_v2(&dc,bytes);else c->alloc_rc[2]=-1;
        if(!c->alloc_rc[0])(*allocs)++;
        if(!c->alloc_rc[1])(*allocs)++;
        if(!c->alloc_rc[2])(*allocs)++;
        if(da)c->htod_rc=API.cuMemcpyHtoD_v2(da,ha,bytes);else c->htod_rc=-1;
        if(db&&!c->htod_rc)c->htod_rc=API.cuMemcpyHtoD_v2(db,hb,bytes);
        if(!c->htod_rc&&da&&db&&dc){
            unsigned grid=(elements+255u)/256u;
            args[0]=&da;args[1]=&db;args[2]=&dc;args[3]=&elements;
            c->launch_rc=API.cuLaunchKernel(fn,grid,1,1,256,1,1,0,NULL,args,NULL);
        }else c->launch_rc=-1;
        if(!c->launch_rc)c->sync_rc=API.cuCtxSynchronize();else c->sync_rc=-1;
        if(!c->sync_rc&&dc)c->dtoh_rc=API.cuMemcpyDtoH_v2(hc,dc,bytes);else c->dtoh_rc=-1;
        if(!c->dtoh_rc)
            if(validate_outputs(ha,hb,hc,elements,&c->mismatches,&c->csum))bad=1;
        c->end_ns=now_ns();
        if(da){c->free_rc[0]=API.cuMemFree_v2(da);if(!c->free_rc[0])(*frees)++;}else c->free_rc[0]=-1;
        if(db){c->free_rc[1]=API.cuMemFree_v2(db);if(!c->free_rc[1])(*frees)++;}else c->free_rc[1]=-1;
        if(dc){c->free_rc[2]=API.cuMemFree_v2(dc);if(!c->free_rc[2])(*frees)++;}else c->free_rc[2]=-1;
        if(c->alloc_rc[0]||c->alloc_rc[1]||c->alloc_rc[2]||c->htod_rc||c->launch_rc||
           c->sync_rc||c->dtoh_rc||c->free_rc[0]||c->free_rc[1]||c->free_rc[2])bad=1;
    }
    free(ha);free(hb);free(hc);
    return bad;
}

static int monotonic_ns_pair(int64_t s,int64_t e){
    return s>=0&&e>s;
}

/* A cycle counts as completed-and-verified only if all three allocs,
 * HtoD, launch, sync and DtoH succeeded, the host comparison found no
 * mismatch, all three frees succeeded, and the interval is monotonic.
 * Partial/failed cycles contribute zero verified bytes -- this is the
 * honest accounting used on every phase instead of assuming the full
 * requested iteration count.  Pure function; covered by the emitter
 * fixture with injected partial/failed cycles. */
static int completed_verified_cycles(const struct cycle_result *c, int n){
    int i,k=0;
    for(i=0;i<n;i++){
        if(c[i].alloc_rc[0]==0&&c[i].alloc_rc[1]==0&&c[i].alloc_rc[2]==0
           &&c[i].htod_rc==0&&c[i].launch_rc==0&&c[i].sync_rc==0
           &&c[i].dtoh_rc==0&&c[i].free_rc[0]==0&&c[i].free_rc[1]==0
           &&c[i].free_rc[2]==0&&c[i].mismatches==0
           &&monotonic_ns_pair(c[i].start_ns,c[i].end_ns))
            k++;
    }
    return k;
}

/* ---- start gate (bind/participate; fixed participants) ---- */
static void gate_init(struct start_gate *g){pthread_mutex_init(&g->mu,NULL);
    pthread_cond_init(&g->cv,NULL);g->total=g->waiting=g->released=0;}
static void gate_arm(struct start_gate *g,int total){pthread_mutex_lock(&g->mu);
    g->total=total;g->waiting++;if(g->waiting>=total)g->released=1;
    pthread_cond_broadcast(&g->cv);pthread_mutex_unlock(&g->mu);}
static void gate_wait(struct start_gate *g) {
    pthread_mutex_lock(&g->mu);
    g->waiting++;
    if (g->waiting >= g->total) g->released = 1;
    pthread_cond_broadcast(&g->cv);
    while (!g->released)
        pthread_cond_wait(&g->cv, &g->mu);
    pthread_mutex_unlock(&g->mu);
}

/* ---- inventory ---- */
static void emit_inventory(int ordinal,const char *uuid,int valid,
                           const char *name,uint64_t total){
    struct jb b;jb_init(&b,512);
    jb_printf(&b,"{\"type\":\"inventory\",\"ordinal\":%d,\"uuid\":",ordinal);
    json_string(&b,uuid);jb_printf(&b,",\"uuid_valid\":%s,\"name\":",valid?"true":"false");
    json_string(&b,name);jb_printf(&b,",\"total_mem\":%llu}",(unsigned long long)total);
    emit(&b);
}

/* ---- solo phase: repeated alloc/compute/free on one card/context ---- */
static void put_cycles_json(struct jb *b,const struct cycle_result *c,int n);

static void run_solo_phase(CUdevice dev,int idx,int ordinal,const char *uuid,
                           const struct config *cfg){
    size_t free_b=0,total_b=0,free_e=0,total_e=0;
    CUcontext ctx=NULL;CUmodule mod=NULL;CUfunction fn=NULL;
    struct cycle_result cyc[MAX_ITEMS];
    uint32_t allocs=0,frees=0,mismatch_total=0;
    uint32_t bytes_checked=0;int completed=0;
    int i,bad=0,ran=0;int64_t s=0,e=0;
    int create_rc=-1,load_rc=-1,unload_rc=-1,destroy_rc=-1;
    int ierc=-1,ixrc=-1;
    memset(cyc,0,sizeof(cyc));
    create_rc=API.cuCtxCreate_v2(&ctx,0,dev);
    if(create_rc){
        record_failuref("solo_create","ord=%d rc=%d",ordinal,create_rc);
        bad=1;
    } else {
        /* cuMemGetInfo acts on the CURRENT context: capture the baseline
         * only after CtxCreate succeeded (v2 called it first and got
         * rc 201), and before any allocation. */
        ierc=API.cuMemGetInfo_v2(&free_b,&total_b);
        if(ierc){record_failuref("solo_meminfo_enter","ord=%d rc=%d",
                                 ordinal,ierc);bad=1;}
    }
    if(!bad){
        load_rc=load_add_kernel(&mod,&fn);
        if(load_rc){record_failuref("solo_load","ord=%d rc=%d",ordinal,
                                    load_rc);bad=1;}
    }
    if(!bad){
        ran=1;s=now_ns();
        int rr=run_cycles(fn,cfg->elements,cfg->cycles,ROLE_SOLO,0,
                         (uint32_t)idx,0,cyc,&allocs,&frees);
        e=now_ns();
        if(rr<0){record_failure("solo_cycles");bad=1;}
        else if(rr){record_failuref("solo_cycles","ordinal=%d",ordinal);bad=1;}
    }
    /* Actual completed, verified cycles only -- never the requested count.
     * On a failed module load this stays zero (v2 falsely reported 8 MiB). */
    if(ran){
        completed=completed_verified_cycles(cyc,cfg->cycles);
        bytes_checked=(uint32_t)completed*(uint32_t)cfg->elements*4u;
        for(i=0;i<cfg->cycles;i++)mismatch_total+=cyc[i].mismatches;
    }
    /* Exit MemGetInfo while the context is still current and BEFORE its
     * destruction; independent return codes for unload and destroy. */
    if(ctx)ixrc=API.cuMemGetInfo_v2(&free_e,&total_e);
    if(mod){
        unload_rc=API.cuModuleUnload(mod);
        if(unload_rc){record_failuref("solo_unload","ord=%d rc=%d",ordinal,
                                      unload_rc);bad=1;}
    }
    if(ctx){
        destroy_rc=API.cuCtxDestroy_v2(ctx);
        if(destroy_rc){record_failuref("solo_destroy","ord=%d rc=%d",ordinal,
                                       destroy_rc);bad=1;}
    }
    {
        struct jb b;jb_init(&b,2048);
        jb_printf(&b,"{\"type\":\"worker\",\"phase\":\"solo\",\"gpu\":%d,\"ordinal\":%d,\"uuid\":",idx,ordinal);
        json_string(&b,uuid);
        jb_printf(&b,",\"pid\":%ld,\"iterations\":%d,\"completed_iterations\":%d,"
                  "\"elements\":%u,\"allocs\":%u,\"frees\":%u,"
                  "\"bytes_checked\":%u,\"mismatches\":%u,\"create_rc\":%d,"
                  "\"load_rc\":%d,\"unload_rc\":%d,\"destroy_rc\":%d,"
                  "\"info_enter_rc\":%d,\"info_exit_rc\":%d,"
                  "\"free_enter\":%llu,\"free_exit\":%llu,"
                  "\"start_ns\":%lld,\"end_ns\":%lld",
                  (long)getpid(),cfg->cycles,completed,cfg->elements,
                  allocs,frees,bytes_checked,mismatch_total,create_rc,
                  load_rc,unload_rc,destroy_rc,ierc,ixrc,
                  (unsigned long long)free_b,(unsigned long long)free_e,
                  (long long)s,(long long)e);
        put_cycles_json(&b,cyc,cfg->cycles);
        jb_put(&b,"}");
        emit(&b);
    }
    if(bad)atomic_store(&global_failed,1);
}

/* ---- thread phase: independent contexts, one card each, concurrent ---- */
struct thread_args{CUdevice dev;int index,ordinal;const char *uuid;
    unsigned elements;int cycles;struct start_gate *gate;
    pthread_t handle;struct worker_result result;};

static void *thread_worker(void *arg){
    struct thread_args *a=arg;CUcontext ctx=NULL;CUmodule mod=NULL;CUfunction fn=NULL;
    uint32_t allocs=0,frees=0;struct worker_result *w=&a->result;
    int bad=0,r,completed=0;
    memset(w,0,sizeof(*w));
    w->worker=a->index;w->ordinal=a->ordinal;strncpy(w->uuid,a->uuid,UUID_LEN-1);
    w->pid=getpid();w->tid=gettid_();w->elements=(int)a->elements;w->cycles=a->cycles;
    w->create_rc=w->load_rc=w->unload_rc=w->destroy_rc=-1;
    w->create_rc=API.cuCtxCreate_v2(&ctx,0,a->dev);
    if(!w->create_rc)w->load_rc=load_add_kernel(&mod,&fn);
    if(w->create_rc||w->load_rc){atomic_store(&global_failed,1);bad=1;}
    w->ready_ns=now_ns();
    gate_wait(a->gate);
    if(!bad){
        w->start_ns=now_ns();
        r=run_cycles(fn,a->elements,a->cycles,ROLE_THREAD,0,
                     (uint32_t)a->index,0,w->cycle,&allocs,&frees);
        w->end_ns=now_ns();
        w->allocs=allocs;w->frees=frees;
        completed=completed_verified_cycles(w->cycle,a->cycles);
        w->completed_cycles=completed;
        w->bytes_checked=(uint32_t)completed*(uint32_t)a->elements*4u;
        {unsigned i;for(i=0;i<(unsigned)a->cycles;i++)
            w->mismatches+=w->cycle[i].mismatches;}
        if(r)atomic_store(&global_failed,1);
    }
    if(mod){
        w->unload_rc=API.cuModuleUnload(mod);
        if(w->unload_rc)atomic_store(&global_failed,1);
    }
    if(ctx){
        w->destroy_rc=API.cuCtxDestroy_v2(ctx);
        if(w->destroy_rc)atomic_store(&global_failed,1);
    }
    return NULL;
}

static void emit_thread_worker(const struct worker_result *w){
    struct jb b;jb_init(&b,2048);
    jb_printf(&b,"{\"type\":\"worker\",\"phase\":\"thread\",\"worker\":%d,\"ordinal\":%d,\"uuid\":",w->worker,w->ordinal);
    json_string(&b,w->uuid);
    jb_printf(&b,",\"pid\":%ld,\"tid\":%ld,\"cycles\":%d,"
              "\"completed_cycles\":%d,\"elements\":%d,"
              "\"allocs\":%u,\"frees\":%u,\"bytes_checked\":%u,"
              "\"mismatches\":%u,\"create_rc\":%d,\"load_rc\":%d,"
              "\"unload_rc\":%d,\"destroy_rc\":%d,\"ready_ns\":%lld,"
              "\"start_ns\":%lld,\"end_ns\":%lld",
              (long)w->pid,(long)w->tid,w->cycles,w->completed_cycles,
              w->elements,w->allocs,w->frees,w->bytes_checked,w->mismatches,
              w->create_rc,w->load_rc,w->unload_rc,w->destroy_rc,
              (long long)w->ready_ns,(long long)w->start_ns,(long long)w->end_ns);
    put_cycles_json(&b,w->cycle,w->cycles);
    jb_put(&b,"}");
    emit(&b);
}

static void run_thread_phase(const struct config *cfg,const CUdevice *devs,
                             const char **uuids){
    struct thread_args args[MAX_THREADS+2];
    struct start_gate gate;int i,joined=0;
    gate_init(&gate);
    for(i=0;i<cfg->threads;i++){
        args[i].dev=devs[i%cfg->ngpu];args[i].index=i;
        args[i].ordinal=cfg->ordinals[i%cfg->ngpu];args[i].uuid=uuids[i%cfg->ngpu];
        args[i].elements=cfg->elements;args[i].cycles=cfg->thread_cycles;
        args[i].gate=&gate;
        if(pthread_create(&args[i].handle,NULL,thread_worker,&args[i])!=0){
            record_failuref("thread_pthread_create","index=%d",i);break;}
        joined++;
    }
    gate_arm(&gate,joined);
    for(i=0;i<joined;i++)pthread_join(args[i].handle,NULL);
    for(i=0;i<joined;i++)emit_thread_worker(&args[i].result);
    if(joined<cfg->threads)atomic_store(&global_failed,1);
}

/* ---- peer phase -------------------------------------------------------- */

static void run_one_peer(CUdevice da_dev,CUdevice db_dev,int ia,int ib,
                         int oa,int ob,const char *ua,const char *ub,
                         unsigned elements){
    int can=-1,can_rc,en_rc=-1,dto_d=-1,htod=-1,dtoh=-1;
    int ca_rc=-1,cb_rc=-1,db_destroy=-1,da_destroy=-1;
    CUcontext ca=NULL,cb=NULL;CUdeviceptr da=0,db=0;
    uint32_t mismatches=0,csum=0;size_t bytes=(size_t)elements*4;
    float *ha=NULL,*hb=NULL;uint32_t seed=cycle_seed(5,0,(uint32_t)ia,0,(uint32_t)ib);
    int64_t s=now_ns(),e=0;int supported=0,attempted=0;
    can_rc=API.cuDeviceCanAccessPeer(&can,da_dev,db_dev);
    if(can_rc||can<0)goto emit;
    if(!can)goto emit;   /* honest unsupported, no attempt */
    supported=1;
    ca_rc=API.cuCtxCreate_v2(&ca,0,da_dev);
    cb_rc=API.cuCtxCreate_v2(&cb,0,db_dev);   /* current becomes cb */
    if(ca_rc||cb_rc)goto cleanup;
    if(API.cuMemAlloc_v2(&db,bytes))goto cleanup;   /* alloc in B (current) */
    if(API.cuCtxSetCurrent(ca))goto cleanup;         /* switch to A */
    en_rc=API.cuCtxEnablePeerAccess(cb,0);
    if(en_rc)goto cleanup;
    if(API.cuMemAlloc_v2(&da,bytes))goto cleanup;
    ha=malloc(bytes);hb=malloc(bytes);
    if(!ha||!hb)goto cleanup;
    {unsigned j;for(j=0;j<elements;j++)ha[j]=pattern_value(j,seed,0)+
        pattern_value(j,seed,1);}
    htod=API.cuMemcpyHtoD_v2(da,ha,bytes);
    if(!htod)dto_d=API.cuMemcpyDtoD_v2(db,da,bytes);   /* A -> B peer write */
    if(!dto_d)dtoh=API.cuMemcpyDtoH_v2(hb,db,bytes);   /* peer read from A */
    if(!dtoh){unsigned j;for(j=0;j<elements;j++){
        float got=hb[j],want=ha[j];
        if(!isfinite(got)||got!=want){mismatches++;continue;}
        csum+=(uint32_t)(unsigned)got;}}
    attempted=1;
    if(mismatches)atomic_store(&global_failed,1);
cleanup:
    if(ha)free(ha);
    if(hb)free(hb);
    if(da&&API.cuCtxSetCurrent(ca)==0)API.cuMemFree_v2(da);
    /* db belongs to cb: make cb current to free it */
    if(db&&API.cuCtxSetCurrent(cb)==0)API.cuMemFree_v2(db);
    if(ca)API.cuCtxSetCurrent(ca);
    if(cb)db_destroy=API.cuCtxDestroy_v2(cb);   /* non-current destroy */
    if(ca)da_destroy=API.cuCtxDestroy_v2(ca);
    if(attempted&&(en_rc||htod||dto_d||dtoh))
        record_failuref("peer_transfer","%d->%d rc=%d/%d/%d/%d",oa,ob,
                        en_rc,htod,dto_d,dtoh);
emit:
    e=now_ns();
    {
        struct jb b;jb_init(&b,1024);
        jb_printf(&b,"{\"type\":\"peer\",\"from\":%d,\"to\":%d,\"from_ordinal\":%d,\"to_ordinal\":%d,\"from_uuid\":",
                  ia,ib,oa,ob);json_string(&b,ua);
        jb_printf(&b,",\"to_uuid\":");json_string(&b,ub);
        jb_printf(&b,",\"can_rc\":%d,\"can_access\":%s,\"supported\":%s,"
                  "\"create_a_rc\":%d,\"create_b_rc\":%d,"
                  "\"destroy_a_rc\":%d,\"destroy_b_rc\":%d,\"enable_rc\":",
                  can_rc,can?"true":"false",supported?"true":"false",
                  ca_rc,cb_rc,da_destroy,db_destroy);
        if(en_rc<0)jb_printf(&b,"null");else jb_printf(&b,"%d",en_rc);
        jb_printf(&b,",\"htod_rc\":");if(htod<0)jb_put(&b,"null");else jb_printf(&b,"%d",htod);
        jb_printf(&b,",\"dto_d_rc\":");if(dto_d<0)jb_put(&b,"null");else jb_printf(&b,"%d",dto_d);
        jb_printf(&b,",\"dtoh_rc\":");if(dtoh<0)jb_put(&b,"null");else jb_printf(&b,"%d",dtoh);
        jb_printf(&b,",\"elements\":%u,\"bytes\":%llu,\"mismatches\":%u,"
                  "\"csum\":%u,\"start_ns\":%lld,\"end_ns\":%lld}",
                  elements,(unsigned long long)bytes,mismatches,csum,
                  (long long)s,(long long)e);
        emit(&b);
    }
    if(can_rc||(can&&(!attempted))){
        record_failuref("peer_pair","%d->%d can_rc=%d attempted=%d",
                        oa,ob,can_rc,attempted);
    }
}

static void run_peer_phase(const struct config *cfg,const CUdevice *devs,
                           const char **uuids){
    int i,j;
    for(i=0;i<cfg->ngpu;i++)
        for(j=0;j<cfg->ngpu;j++)
            if(i!=j)
                run_one_peer(devs[i],devs[j],i,j,
                             cfg->ordinals[i],cfg->ordinals[j],
                             uuids[i],uuids[j],cfg->elements);
}

/* ---- child phase: strict 16-field wire protocol + handshake (proven
 * v5 design; fork+exec fresh, ready AFTER cuInit/context/JIT) ----------- */

static void put_cycles_json(struct jb *b, const struct cycle_result *c, int n);

static int any_killed(const struct child_observation *obs,size_t n){
    size_t i;for(i=0;i<n;i++)if(obs[i].killed)return 1;return 0;}
static const char *uuid_for_ordinal(const struct config *cfg,
                                    const char **uuids,int ord){
    int k;for(k=0;k<cfg->ngpu;k++)if(cfg->ordinals[k]==ord)return uuids[k];
    return uuids[0];}

static int proto_ll(const char *s,long long lo,long long hi,long long *out){
    char *end;long long v;
    if(!*s)return -1;
    errno=0;v=strtoll(s,&end,10);
    if(errno||*end!='\0'||v<lo||v>hi)return -1;
    *out=v;return 0;}

static int proto_cycle(const char *tok,struct cycle_result *c){
    char buf[CHILD_CYCLE_TOKEN_MAX+1],*save=NULL,*field;
    long long vals[CHILD_CYCLE_FIELDS];int k=0,j,commas=0;size_t len=strlen(tok);
    memset(c,0,sizeof(*c));
    if(len==0||len>=sizeof(buf)||tok[0]==','||tok[len-1]==',')return -1;
    for(size_t i=0;i<len;i++)if(tok[i]==',')commas++;
    if(commas!=CHILD_CYCLE_FIELDS-1)return -1;
    memcpy(buf,tok,len+1);
    for(field=strtok_r(buf,",",&save);field;field=strtok_r(NULL,",",&save)){
        long long v;if(k>=CHILD_CYCLE_FIELDS)return -1;
        if(k==0||k==1||k==12||k==13){if(proto_ll(field,0,UINT32_MAX,&v))return -1;}
        else if(k==14||k==15){if(proto_ll(field,0,INT64_MAX,&v))return -1;}
        else{if(proto_ll(field,INT_MIN,INT_MAX,&v))return -1;}
        vals[k++]=v;}
    if(k!=CHILD_CYCLE_FIELDS||vals[15]<=vals[14])return -1;
    c->idx=(uint32_t)vals[0];c->seed=(uint32_t)vals[1];
    for(j=0;j<3;j++)c->alloc_rc[j]=(int)vals[2+j];
    c->htod_rc=(int)vals[5];c->launch_rc=(int)vals[6];c->sync_rc=(int)vals[7];
    c->dtoh_rc=(int)vals[8];
    for(j=0;j<3;j++)c->free_rc[j]=(int)vals[9+j];
    c->mismatches=(uint32_t)vals[12];c->csum=(uint32_t)vals[13];
    c->start_ns=vals[14];c->end_ns=vals[15];return 0;}

static int uuid_wire_valid(const char *s){
    static const int dash[4]={8,13,18,23};int i,d=0;
    if(strncmp(s,"GPU-",4))return -1;
    s+=4;
    if(strlen(s)!=36)return -1;
    for(i=0;i<36;i++){
        if(d<4&&i==dash[d]){
            if(s[i]!='-')return -1;
            d++;
            continue;
        }
        if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))
            return -1;
    }
    return d==4?0:-1;}

enum{H_SEQ,H_ORD,H_PID,H_PPID,H_EL,H_CY,H_AL,H_FR,H_MM,H_RC,H_LR,H_UR,H_DR,H_ST,H_EN,H_UUID,H_COUNT};
static const char *const header_names[H_COUNT]={"seq","ord","pid","ppid","el","cy","al","fr","mm","rc","lr","ur","dr","st","en","uuid"};
static int header_key(const char *tok){int i;
    if(!strncmp(tok,"c=",2))return -1;
    for(i=0;i<H_COUNT;i++){size_t l=strlen(header_names[i]);
        if(!strncmp(tok,header_names[i],l)&&tok[l]=='=')return i;}
    return -2;}

static int parse_child_line(const char *line,struct child_observation *o){
    char *copy,*save=NULL,*tok;int seen[H_COUNT]={0};int have=0,n=0,bad=0;
    long long hv[H_COUNT];const char *uuidv=NULL;
    if(!line)return -1;
    {size_t len=strlen(line);
     if(len<7||line[len-1]!='\n'||memchr(line,'\n',len-1)||
        strncmp(line,"LCYC1 ",6))return -1;}
    copy=strdup(line);if(!copy)return -1;copy[strlen(copy)-1]='\0';
    tok=strtok_r(copy," ",&save);if(!tok||strcmp(tok,"LCYC1")){free(copy);return -1;}
    while((tok=strtok_r(NULL," ",&save))){
        int id=header_key(tok);
        if(id==-1){char *cs=NULL,*ct;
            if(have){bad=1;break;}have=1;
            for(ct=strtok_r(tok+2,";",&cs);ct;ct=strtok_r(NULL,";",&cs)){
                if(n>=MAX_ITEMS||proto_cycle(ct,&o->cycle[n])){bad=1;break;}
                n++;
            }
            if(bad) break;
            continue;
        }
        if(id<0||seen[id]++){bad=1;break;}
        if(id==H_UUID){uuidv=tok+5;continue;}
        {char *val=strchr(tok,'=')+1;long long lo=INT_MIN,hi=INT_MAX;
         switch(id){
         case H_SEQ:lo=0;hi=MAX_CHILDREN-1;break;
         case H_ORD:lo=0;hi=MAX_GPUS-1;break;
         case H_PID:case H_PPID:lo=1;hi=INT_MAX;break;
         case H_EL:lo=4096;hi=1<<20;break;
         case H_CY:lo=1;hi=MAX_ITEMS;break;
         case H_AL:case H_FR:case H_MM:lo=0;hi=UINT32_MAX;break;
         case H_ST:case H_EN:lo=0;hi=INT64_MAX;break;}
         if(proto_ll(val,lo,hi,&hv[id])){bad=1;break;}}}
    if(!bad){int i;for(i=0;i<H_COUNT;i++)if(!seen[i])bad=1;}
    if(!bad&&((long long)n!=hv[H_CY]||(int)hv[H_SEQ]!=o->seq))bad=1;
    if(!bad&&uuid_wire_valid(uuidv))bad=1;
    if(!bad){int i;
        snprintf(o->uuid,sizeof(o->uuid),"%s",uuidv);
        o->ordinal=(int)hv[H_ORD];o->reported_pid=(pid_t)hv[H_PID];
        o->reported_ppid=(pid_t)hv[H_PPID];o->elements=(int)hv[H_EL];
        o->cycles=(int)hv[H_CY];o->allocs=(uint32_t)hv[H_AL];
        o->frees=(uint32_t)hv[H_FR];o->mismatches=(uint32_t)hv[H_MM];
        o->create_rc=(int)hv[H_RC];o->load_rc=(int)hv[H_LR];
        o->unload_rc=(int)hv[H_UR];o->destroy_rc=(int)hv[H_DR];
        o->start_ns=hv[H_ST];o->end_ns=hv[H_EN];
        for(i=0;i<n;i++){o->cycle[i].role=ROLE_CHILD;
            o->cycle[i].proc=(uint32_t)o->seq+1;o->cycle[i].worker=0;o->cycle[i].round=0;}
        if(o->cycle[0].start_ns<o->start_ns||
           o->cycle[n-1].end_ns>o->end_ns)bad=1;}
    free(copy);return bad?-1:0;
}

struct child_line_input{int seq,ordinal,elements,cycles;pid_t pid,ppid;
    uint32_t allocs,frees,mismatches;int create_rc,load_rc,unload_rc,destroy_rc;
    int64_t start_ns,end_ns;const char *uuid;struct cycle_result *cyc;};

static int format_child_line(char *line,size_t cap,const struct child_line_input *in){
    size_t z=0;int w,i;
    w=snprintf(line+z,cap-z,
        "LCYC1 seq=%d ord=%d pid=%ld ppid=%ld el=%d cy=%d al=%u fr=%u mm=%u "
        "rc=%d lr=%d ur=%d dr=%d st=%lld en=%lld uuid=%s",
        in->seq,in->ordinal,(long)in->pid,(long)in->ppid,in->elements,
        in->cycles,in->allocs,in->frees,in->mismatches,in->create_rc,
        in->load_rc,in->unload_rc,in->destroy_rc,(long long)in->start_ns,
        (long long)in->end_ns,in->uuid);
    if(w<0||(size_t)w+1>cap) return -1;
    z+=(size_t)w;
    for(i=0;i<in->cycles;i++){const struct cycle_result *c=&in->cyc[i];
        w=snprintf(line+z,cap-z,"%s%u,%u,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%u,%u,%lld,%lld",
            i==0?" c=":";",c->idx,c->seed,c->alloc_rc[0],c->alloc_rc[1],c->alloc_rc[2],
            c->htod_rc,c->launch_rc,c->sync_rc,c->dtoh_rc,c->free_rc[0],
            c->free_rc[1],c->free_rc[2],c->mismatches,c->csum,
            (long long)c->start_ns,(long long)c->end_ns);
        if(w<0||(size_t)w+1>cap-z) return -1;
        z+=(size_t)w;
    }
    {size_t rem=cap-z;w=snprintf(line+z,rem,"\n");
     if(w<0||(size_t)w+1>rem)return -1;}return 0;}

static void emit_child_worker(const struct child_observation *o){
    struct jb b;jb_init(&b,1024);
    jb_printf(&b,"{\"type\":\"worker\",\"phase\":\"child\",\"worker\":%d,\"ordinal\":%d,\"uuid\":",o->seq,o->ordinal);
    json_string(&b,o->uuid);
    jb_printf(&b,",\"child_pid\":%ld,\"reported_pid\":%ld,\"reported_ppid\":%ld,"
              "\"received\":%s,\"exec_errno\":%d,\"child_exit\":%d,"
              "\"child_signal\":%d,\"killed\":%s,\"elements\":%d,"
              "\"cycles\":%d,\"completed_cycles\":%d,\"allocs\":%u,"
              "\"frees\":%u,\"bytes_checked\":%u,\"mismatches\":%u,"
              "\"create_rc\":%d,\"load_rc\":%d,\"unload_rc\":%d,"
              "\"destroy_rc\":%d,\"fork_ns\":%lld,\"ready_ns\":%lld,"
              "\"release_ns\":%lld,\"firstbyte_ns\":%lld,\"wait_ns\":%lld,"
              "\"start_ns\":%lld,\"end_ns\":%lld",
              (long)o->child_pid,(long)o->reported_pid,(long)o->reported_ppid,
              o->received?"true":"false",o->exec_errno,o->exit_code,
              o->exit_signal,o->killed?"true":"false",o->elements,o->cycles,
              o->completed_cycles,o->allocs,o->frees,o->bytes_checked,
              o->mismatches,o->create_rc,o->load_rc,
              o->unload_rc,o->destroy_rc,(long long)o->fork_ns,
              (long long)o->ready_ns,(long long)o->release_ns,
              (long long)o->firstbyte_ns,(long long)o->wait_ns,
              (long long)o->start_ns,(long long)o->end_ns);
    put_cycles_json(&b,o->cycle,o->cycles);
    jb_put(&b,"}");emit(&b);
}

static void run_child_phase(const char *self,const struct config *cfg,
                            const char **uuids){
    int gate[2]={-1,-1};
    int *out_r=NULL,*out_w=NULL,*rdy_r=NULL,*rdy_w=NULL,*rel_r=NULL,*rel_w=NULL;
    struct child_observation *obs=NULL;struct pollfd *pfds=NULL;
    char **lines=NULL;size_t *line_len=NULL;
    int n=cfg->children,i,forked=0;int64_t deadline;
    obs=calloc((size_t)n,sizeof(*obs));pfds=calloc((size_t)n,sizeof(*pfds));
    lines=calloc((size_t)n,sizeof(*lines));line_len=calloc((size_t)n,sizeof(*line_len));
    out_r=calloc((size_t)n,sizeof(*out_r));out_w=calloc((size_t)n,sizeof(*out_w));
    rdy_r=calloc((size_t)n,sizeof(*rdy_r));rdy_w=calloc((size_t)n,sizeof(*rdy_w));
    rel_r=calloc((size_t)n,sizeof(*rel_r));rel_w=calloc((size_t)n,sizeof(*rel_w));
    if(!obs||!pfds||!lines||!line_len||!out_r||!out_w||!rdy_r||!rdy_w||!rel_r||!rel_w){
        record_failure("child_calloc");goto out;}
    for(i=0;i<n;i++){
        out_r[i]=out_w[i]=rdy_r[i]=rdy_w[i]=rel_r[i]=rel_w[i]=-1;
        obs[i].seq=i;obs[i].exit_code=-1;
        /* -1 until a parsed child report proves otherwise: a child that
         * died before/at setup (v2 native: exit 8, no report) must not
         * look like create/load/unload/destroy all succeeded. */
        obs[i].create_rc=obs[i].load_rc=obs[i].unload_rc=-1;
        obs[i].destroy_rc=-1;
        obs[i].child_pid=obs[i].reported_pid=obs[i].reported_ppid=-1;
        obs[i].fork_ns=obs[i].ready_ns=obs[i].release_ns=-1;
        obs[i].firstbyte_ns=obs[i].wait_ns=-1;
        obs[i].start_ns=obs[i].end_ns=-1;
        obs[i].ordinal=cfg->ordinals[i%cfg->ngpu];
        snprintf(obs[i].uuid,sizeof(obs[i].uuid),"%s",
                 uuid_for_ordinal(cfg,uuids,obs[i].ordinal));}
    if(pipe(gate)<0){record_failure("child_gate_pipe");goto out;}
    for(i=0;i<n;i++){int a[2],b[2],c[2];
        if(pipe(a)<0||pipe(b)<0||pipe(c)<0){record_failure("child_pipe");break;}
        out_r[i]=a[0];out_w[i]=a[1];rdy_r[i]=b[0];rdy_w[i]=b[1];rel_r[i]=c[0];rel_w[i]=c[1];}
    deadline=now_ms()+cfg->deadline_ms;
    for(i=0;i<n;i++){
        char sord[16],sseq[16],sgate[16],sout[16],srdy[16],srel[16],sel[16],scyc[16],suuid[UUID_LEN];
        pid_t pid;int ord=obs[i].ordinal;
        if(out_w[i]<0||rdy_w[i]<0||rel_r[i]<0)break;
        snprintf(sord,16,"%d",ord);snprintf(sseq,16,"%d",i);
        snprintf(sgate,16,"%d",gate[0]);snprintf(sout,16,"%d",out_w[i]);
        snprintf(srdy,16,"%d",rdy_w[i]);snprintf(srel,16,"%d",rel_r[i]);
        snprintf(sel,16,"%u",cfg->elements);snprintf(scyc,16,"%d",cfg->child_cycles);
        snprintf(suuid,sizeof(suuid),"%s",uuid_for_ordinal(cfg,uuids,ord));
        obs[i].fork_ns=now_ns();pid=fork();
        if(pid<0){record_failure("child_fork");obs[i].fork_ns=0;break;}
        if(pid==0){int fd,top;
            close(gate[1]);
            for(int j=0;j<n;j++){close(out_r[j]);close(rdy_r[j]);close(rel_w[j]);
                if(j!=i){close(out_w[j]);close(rdy_w[j]);}}
            top=(int)sysconf(_SC_OPEN_MAX);if(top<0||top>1024)top=1024;
            for(fd=3;fd<top;fd++)
                if(fd!=gate[0]&&fd!=out_w[i]&&fd!=rdy_w[i]&&fd!=rel_r[i])close(fd);
            execl(self,self,"--child","--ordinal",sord,"--seq",sseq,
                  "--gate-fd",sgate,"--out-fd",sout,"--rdy-fd",srdy,
                  "--rel-fd",srel,"--elements",sel,"--cycles",scyc,
                  "--uuid",suuid,(char*)NULL);
            {char digits[16],buf[48];int e=errno,z=0,k=0,a2,b2;
             do{digits[z++]=(char)('0'+e%10);e/=10;}while(e);
             for(a2=0,b2=z-1;a2<b2;a2++,b2--){char t=digits[a2];digits[a2]=digits[b2];digits[b2]=t;}
             memcpy(buf+k,"LERR1 e=",8);k+=8;memcpy(buf+k,digits,(size_t)z);k+=z;buf[k++]='\n';
             write_pipe_all(out_w[i],buf,(size_t)k);_exit(127);}}
        forked++;obs[i].child_pid=pid;
        close(out_w[i]);out_w[i]=-1;close(rdy_w[i]);rdy_w[i]=-1;close(rel_r[i]);rel_r[i]=-1;}
    for(i=0;i<forked;i++)if(write_pipe_byte(gate[1],'g')<0&&errno!=EPIPE)
        record_failuref("child_gate_write","seq=%d errno=%d",i,errno);
    close(gate[1]);gate[1]=-1;close(gate[0]);gate[0]=-1;
    /* ready handshake */
    for(i=0;i<forked;i++){fcntl(rdy_r[i],F_SETFL,O_NONBLOCK);
        pfds[i].fd=rdy_r[i];pfds[i].events=POLLIN;}
    {int ready=0;for(;;){int open_fds=0,pr,i2,wait_ms;
        for(i=0;i<forked;i++)if(pfds[i].fd>=0)open_fds++;
        if(!open_fds||now_ms()>=deadline)break;
        wait_ms=(int)(deadline-now_ms());if(wait_ms>1000)wait_ms=1000;
        pr=poll(pfds,(nfds_t)forked,wait_ms);
        if(pr<0){if(errno==EINTR)continue;record_failure("child_ready_poll");break;}
        for(i2=0;i2<forked;i2++){char byte=0;ssize_t r;
            if(pfds[i2].fd<0||!(pfds[i2].revents&(POLLIN|POLLHUP|POLLERR)))continue;
            r=read(pfds[i2].fd,&byte,1);
            if(r==1){if(byte=='R'){obs[i2].ready_ns=now_ns();ready++;}
                else record_failuref("child_ready_badbyte","seq=%d",i2);
                pfds[i2].fd=-1;}
            else if(r==0||(r<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK))pfds[i2].fd=-1;
            pfds[i2].revents=0;}}
        if(ready!=forked)record_failuref("child_ready","ready=%d of %d",ready,forked);}
    {int64_t rel=now_ns();
     for(i=0;i<forked;i++){obs[i].release_ns=rel;
         if(rel_w[i]>=0){
             if(write_pipe_byte(rel_w[i],'g')<0&&errno!=EPIPE)
                 record_failuref("child_release_write","seq=%d errno=%d",i,errno);
             close(rel_w[i]);rel_w[i]=-1;
         }}}
    for(i=0;i<forked;i++){fcntl(out_r[i],F_SETFL,O_NONBLOCK);
        pfds[i].fd=out_r[i];pfds[i].events=POLLIN;}
    for(;;){int open_fds=0,pr,i2,wait_ms;
        for(i=0;i<forked;i++)if(pfds[i].fd>=0)open_fds++;
        if(!open_fds||now_ms()>=deadline)break;
        wait_ms=(int)(deadline-now_ms());if(wait_ms>1000)wait_ms=1000;
        pr=poll(pfds,(nfds_t)forked,wait_ms);
        if(pr<0){if(errno==EINTR)continue;record_failure("child_poll");break;}
        for(i2=0;i2<forked;i2++){ssize_t r;char chunk[1024];
            if(pfds[i2].fd<0||!(pfds[i2].revents&(POLLIN|POLLHUP|POLLERR)))continue;
            while((r=read(pfds[i2].fd,chunk,sizeof(chunk)))>0){
                size_t old=line_len[i2];char*p=realloc(lines[i2],old+(size_t)r+1);
                if(!p){record_failure("child_line_realloc");break;}
                lines[i2]=p;memcpy(p+old,chunk,(size_t)r);line_len[i2]=old+(size_t)r;
                p[line_len[i2]]='\0';
                if(memchr(chunk,'\n',(size_t)r)&&obs[i2].firstbyte_ns<0)obs[i2].firstbyte_ns=now_ns();}
            if(r==0)pfds[i2].fd=-1;
            else if(r<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK)pfds[i2].fd=-1;
            pfds[i2].revents=0;}}
    for(i=0;i<forked;i++){if(!lines[i])continue;
        if(!strncmp(lines[i],"LCYC1 ",6)){
            if(parse_child_line(lines[i],&obs[i])==0){
                obs[i].received=1;
                obs[i].completed_cycles=
                    completed_verified_cycles(obs[i].cycle,obs[i].cycles);
                obs[i].bytes_checked=
                    (uint32_t)obs[i].completed_cycles*
                    (uint32_t)obs[i].elements*4u;
            } else record_failuref("child_parse","seq=%d",i);}
        else if(!strncmp(lines[i],"LERR1 ",6)){char *e=strstr(lines[i],"e="),*nl=strchr(lines[i],'\n');
            long long code=0;if(e&&nl){char buf[32];size_t l=(size_t)(nl-(e+2));
                if(l<sizeof(buf)){memcpy(buf,e+2,l);buf[l]='\0';
                    if(proto_ll(buf,INT_MIN,INT_MAX,&code)==0)obs[i].exec_errno=(int)code;}}
            record_failuref("child_exec","seq=%d errno=%d",i,obs[i].exec_errno);}
        else record_failuref("child_garbage","seq=%d",i);}
    for(i=0;i<forked;i++){int status=0;pid_t r=0;
        while(now_ms()<deadline){pid_t q=waitpid(obs[i].child_pid,&status,WNOHANG);
            if(q==obs[i].child_pid){r=q;break;}
            if(q<0&&errno!=EINTR) break;
            usleep(5000);
        }
        if(r!=obs[i].child_pid){pid_t q;
            kill(obs[i].child_pid,SIGKILL);obs[i].killed=1;
            do q=waitpid(obs[i].child_pid,&status,0);while(q<0&&errno==EINTR);
            if(q==obs[i].child_pid)r=q;}
        obs[i].wait_ns=now_ns();
        if(r==obs[i].child_pid){
            if(WIFEXITED(status))obs[i].exit_code=WEXITSTATUS(status);
            else if(WIFSIGNALED(status))obs[i].exit_signal=WTERMSIG(status);}}
    for(i=0;i<forked;i++)emit_child_worker(&obs[i]);
    if(forked<n||any_killed(obs,(size_t)forked))atomic_store(&global_failed,1);
out:
    for(i=0;i<n;i++){
        if(out_r&&out_r[i]>=0) close(out_r[i]);
        if(out_w&&out_w[i]>=0) close(out_w[i]);
        if(rdy_r&&rdy_r[i]>=0) close(rdy_r[i]);
        if(rdy_w&&rdy_w[i]>=0) close(rdy_w[i]);
        if(rel_r&&rel_r[i]>=0) close(rel_r[i]);
        if(rel_w&&rel_w[i]>=0) close(rel_w[i]);
    }
    if(gate[0]>=0) close(gate[0]);
    if(gate[1]>=0) close(gate[1]);
    free(obs);free(pfds);free(lines);free(line_len);
    free(out_r);free(out_w);free(rdy_r);free(rdy_w);free(rel_r);free(rel_w);
}

/* cycle JSON (shared by child emitter) */
static void put_cycles_json(struct jb *b,const struct cycle_result *c,int n){
    int i,j;
    jb_printf(b,",\"cycles_detail\":[");
    for(i=0;i<n;i++){
        if(i)jb_put(b,",");
        jb_printf(b,"{\"cycle\":%u,\"seed\":%u,\"role\":%u,\"proc\":%u,\"worker\":%u,\"round\":%u,\"alloc_rc\":[",
                  c[i].idx,c[i].seed,c[i].role,c[i].proc,c[i].worker,c[i].round);
        for(j=0;j<3;j++)jb_printf(b,"%s%d",j?",":"",c[i].alloc_rc[j]);
        jb_printf(b,"],\"htod_rc\":%d,\"launch_rc\":%d,\"sync_rc\":%d,\"dtoh_rc\":%d,\"free_rc\":[",
                  c[i].htod_rc,c[i].launch_rc,c[i].sync_rc,c[i].dtoh_rc);
        for(j=0;j<3;j++)jb_printf(b,"%s%d",j?",":"",c[i].free_rc[j]);
        jb_printf(b,"],\"mismatches\":%u,\"csum\":%u,\"start_ns\":%lld,\"end_ns\":%lld}",
                  c[i].mismatches,c[i].csum,(long long)c[i].start_ns,(long long)c[i].end_ns);}
    jb_put(b,"]");
}

/* ---- child mode ---- */
static int parse_int_strict(const char *s, long lo, long hi, int *out);
static int parse_ulong_strict(const char *s, unsigned long lo,
                              unsigned long hi, unsigned long *out);

static int child_main(int argc,char **argv){
    int ordinal=-1,seq=-1,gate_fd=-1,out_fd=-1,cycles=-1,rdy_fd=-1,rel_fd=-1;
    unsigned elements=0;char expect_uuid[UUID_LEN]="";
    CUdevice dev;CUcontext ctx=NULL;CUmodule mod=NULL;CUfunction fn=NULL;
    struct cycle_result cyc[MAX_ITEMS];uint32_t allocs=0,frees=0,mismatches=0;
    int create_rc=-1,load_rc=-1,unload_rc=-1,destroy_rc=-1;
    int rc,i,bad=0,gate_read,rel_read;int64_t start_ns=0,end_ns;
    char line[CHILD_LINE_MAX];char gate_byte,rel_byte;
    unsigned char uuid_bytes[16]={0};char uuid[UUID_LEN];
    int iv;
    unsigned long elv;
    for(i=1;i<argc;i++){
        /* child_main receives the same argv as main; the mode flag has no
         * value and is accepted, not rejected. */
        if(!strcmp(argv[i],"--child"))
            continue;
        if(!strcmp(argv[i],"--ordinal")&&i+1<argc){
            if(parse_int_strict(argv[++i],0,MAX_GPUS-1,&iv))return 2;
            ordinal=iv;
        }
        else if(!strcmp(argv[i],"--seq")&&i+1<argc){
            if(parse_int_strict(argv[++i],0,MAX_CHILDREN-1,&iv))return 2;
            seq=iv;
        }
        else if(!strcmp(argv[i],"--gate-fd")&&i+1<argc){
            if(parse_int_strict(argv[++i],0,65535,&iv))return 2;
            gate_fd=iv;
        }
        else if(!strcmp(argv[i],"--out-fd")&&i+1<argc){
            if(parse_int_strict(argv[++i],0,65535,&iv))return 2;
            out_fd=iv;
        }
        else if(!strcmp(argv[i],"--rdy-fd")&&i+1<argc){
            if(parse_int_strict(argv[++i],0,65535,&iv))return 2;
            rdy_fd=iv;
        }
        else if(!strcmp(argv[i],"--rel-fd")&&i+1<argc){
            if(parse_int_strict(argv[++i],0,65535,&iv))return 2;
            rel_fd=iv;
        }
        else if(!strcmp(argv[i],"--elements")&&i+1<argc){
            if(parse_ulong_strict(argv[++i],4096,1u<<20,&elv))return 2;
            elements=(unsigned)elv;
        }
        else if(!strcmp(argv[i],"--cycles")&&i+1<argc){
            if(parse_int_strict(argv[++i],1,MAX_ITEMS,&iv))return 2;
            cycles=iv;
        }
        else if(!strcmp(argv[i],"--uuid")&&i+1<argc)
            snprintf(expect_uuid,sizeof(expect_uuid),"%s",argv[++i]);
        else return 2;
    }
    if(ordinal<0||seq<0||gate_fd<0||out_fd<0||rdy_fd<0||rel_fd<0||
       cycles<=0||cycles>MAX_ITEMS||elements==0)return 2;
    do{gate_read=read(gate_fd,&gate_byte,1);}while(gate_read<0&&errno==EINTR);
    if(gate_read!=1)return 3;
    rc=API.cuInit(0);if(rc){fprintf(stderr,"child cuInit rc=%d\n",rc);return 4;}
    rc=API.cuDeviceGet(&dev,ordinal);if(rc){fprintf(stderr,"child dev rc=%d\n",rc);return 5;}
    if(API.cuDeviceGetUuid(uuid_bytes,dev)){fprintf(stderr,"child uuid\n");return 5;}
    uuid_format(uuid,uuid_bytes);
    if(expect_uuid[0]&&strcmp(uuid,expect_uuid)){fprintf(stderr,"child uuid mismatch\n");return 6;}
    create_rc=API.cuCtxCreate_v2(&ctx,0,dev);
    if(create_rc){fprintf(stderr,"child create rc=%d\n",create_rc);return 7;}
    load_rc=load_add_kernel(&mod,&fn);
    if(load_rc){API.cuCtxDestroy_v2(ctx);return 8;}
    {size_t done=0;while(done<1){ssize_t w=write(rdy_fd,"R",1);
        if(w<0&&errno==EINTR) continue;
        if(w!=1){API.cuModuleUnload(mod);API.cuCtxDestroy_v2(ctx);return 13;}
        done+=(size_t)w;}}
    do{rel_read=read(rel_fd,&rel_byte,1);}while(rel_read<0&&errno==EINTR);
    if(rel_read!=1){API.cuModuleUnload(mod);API.cuCtxDestroy_v2(ctx);return 12;}
    start_ns=now_ns();
    bad=run_cycles(fn,elements,cycles,ROLE_CHILD,(uint32_t)(seq+1),0,0,
                   cyc,&allocs,&frees);
    end_ns=now_ns();
    if(bad<0){API.cuModuleUnload(mod);API.cuCtxDestroy_v2(ctx);
        write_pipe_all(out_fd,"LERR1 e=-1 h=1\n",16);return 11;}
    for(i=0;i<cycles;i++)mismatches+=cyc[i].mismatches;
    unload_rc=API.cuModuleUnload(mod);destroy_rc=API.cuCtxDestroy_v2(ctx);
    if(unload_rc||destroy_rc)bad=1;
    {struct child_line_input in;memset(&in,0,sizeof(in));
     in.seq=seq;in.ordinal=ordinal;in.pid=getpid();in.ppid=getppid();
     in.elements=(int)elements;in.cycles=cycles;in.allocs=allocs;in.frees=frees;
     in.mismatches=mismatches;in.create_rc=create_rc;in.load_rc=load_rc;
     in.unload_rc=unload_rc;in.destroy_rc=destroy_rc;
     in.start_ns=start_ns;in.end_ns=end_ns;in.uuid=uuid;in.cyc=cyc;
     if(format_child_line(line,sizeof(line),&in)!=0){fprintf(stderr,"report large\n");return 9;}}
    write_pipe_all(out_fd,line,strlen(line));
    return bad?1:0;
}

/* ---- driver ---- */
static void usage(const char *p){
    fprintf(stderr,
      "usage: %s [--iterations 1..32] [--thread-cycles 1..32] "
      "[--child-cycles 1..32] [--threads 4..32] [--children 1..32] "
      "[--elements 4096..1048576] [--deadline-ms 500..60000] "
      "[--gpus 0,2,..]   (--cycles is a deprecated alias for --iterations)\n"
      "       %s --child --ordinal N --seq N --gate-fd FD --out-fd FD "
      "--rdy-fd FD --rel-fd FD --elements N --cycles N [--uuid GPU-...]\n",
      p,p);}

/* Strict whole-string integer conversion.  Unlike atoi/strtol without an
 * end pointer, trailing junk, signs and overflow/wrap are all rejected.
 * Used by both the parent CLI and the child argv parser, so malformed
 * arguments never silently become 0.  Return 0 on success. */
static int first_char_decimal(const char *s) {
    /* strtol/strtoul accept leading whitespace and a '+' sign; strict
     * argv integers must start with an ASCII digit ('-' is handled by the
     * signed caller below). */
    if (!s || !*s)
        return -1;
    if (s[0] >= '0' && s[0] <= '9')
        return 0;
    if (s[0] == '-' && s[1] >= '0' && s[1] <= '9')
        return 0;
    return -1;
}

static int parse_int_strict(const char *s, long lo, long hi, int *out) {
    char *end;
    long v;
    if (first_char_decimal(s))
        return -1;
    errno = 0;
    v = strtol(s, &end, 10);
    if (errno != 0 || *end != '\0' || v < lo || v > hi)
        return -1;
    *out = (int)v;
    return 0;
}

static int parse_ulong_strict(const char *s, unsigned long lo,
                              unsigned long hi, unsigned long *out) {
    char *end;
    unsigned long v;
    if (!s || !(s[0] >= '0' && s[0] <= '9'))
        return -1;
    errno = 0;
    v = strtoul(s, &end, 10);
    if (errno != 0 || *end != '\0' || v < lo || v > hi)
        return -1;
    *out = v;
    return 0;
}

/* Parse an explicit "--gpus 0,2,3" ordinal list.  Manual tokenization:
 * strtok_r would silently swallow empty fields, so leading/trailing/
 * repeated commas, junk, overflow, out-of-range values and duplicates are
 * all rejected explicitly.  Returns 0 and fills cfg->ordinals/ngpu on
 * success, -1 on any malformed input. */
static int parse_ordinals(const char *s, struct config *cfg, int count) {
    const char *p;
    cfg->ngpu = 0;
    if (!s || !*s)
        return -1;
    p = s;
    for (;;) {
        const char *comma;
        char buf[16], *end;
        size_t len;
        long v;
        int k;
        comma = strchr(p, ',');
        len = comma ? (size_t)(comma - p) : strlen(p);
        /* Empty token rejects "", ",x" (leading), "x,,y" (repeated) and
         * "x," (trailing). */
        if (len == 0 || len >= sizeof(buf))
            return -1;
        memcpy(buf, p, len);
        buf[len] = '\0';
        if (first_char_decimal(buf))
            return -1;
        errno = 0;
        v = strtol(buf, &end, 10);
        if (errno != 0 || *end != '\0' || v < 0 || v >= count ||
            cfg->ngpu >= MAX_ITEMS)
            return -1;
        for (k = 0; k < cfg->ngpu; k++)
            if (cfg->ordinals[k] == (int)v)
                return -1;
        cfg->ordinals[cfg->ngpu++] = (int)v;
        if (!comma)
            break;
        p = comma + 1;
    }
    return cfg->ngpu > 0 ? 0 : -1;
}

int main(int argc,char **argv){
    struct config cfg={.cycles=DEFAULT_ITERATIONS,.child_cycles=DEFAULT_CHILD_CYCLES,
        .thread_cycles=DEFAULT_THREAD_CYCLES,.ctx_rounds=1,.threads=DEFAULT_THREADS,
        .children=-1,.elements=DEFAULT_ELEMENTS,.deadline_ms=DEFAULT_DEADLINE_MS};
    const char *gpus_arg=NULL;
    int child_mode=0,i;CUdevice *devs=NULL;const char **uuidp=NULL;
    int count=0,init_rc;int64_t begin_ns=0,end_ns;char self[4096]="";
    const char *selfp=self;
    setvbuf(stdout,NULL,_IOLBF,0);signal(SIGPIPE,SIG_IGN);
    for(i=1;i<argc;i++){
        if(!strcmp(argv[i],"--child"))child_mode=1;
        else if(child_mode&&!strcmp(argv[i],"--ordinal")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--seq")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--gate-fd")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--out-fd")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--rdy-fd")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--rel-fd")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--elements")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--cycles")&&i+1<argc)i++;
        else if(child_mode&&!strcmp(argv[i],"--uuid")&&i+1<argc)i++;
        /* Parent CLI: --iterations is the documented name; --cycles is a
         * deprecated alias for the same solo iteration count.  Every value
         * uses strict whole-string conversion (no atoi trailing junk). */
        else if((!strcmp(argv[i],"--iterations")||
                 !strcmp(argv[i],"--cycles"))&&i+1<argc){
            if(parse_int_strict(argv[++i],1,MAX_ITEMS,&cfg.cycles)){
                usage(argv[0]);return 2;}}
        else if(!strcmp(argv[i],"--thread-cycles")&&i+1<argc){
            if(parse_int_strict(argv[++i],1,MAX_ITEMS,&cfg.thread_cycles)){
                usage(argv[0]);return 2;}}
        else if(!strcmp(argv[i],"--child-cycles")&&i+1<argc){
            if(parse_int_strict(argv[++i],1,MAX_ITEMS,&cfg.child_cycles)){
                usage(argv[0]);return 2;}}
        else if(!strcmp(argv[i],"--threads")&&i+1<argc){
            if(parse_int_strict(argv[++i],DEFAULT_THREADS,MAX_THREADS,
                                &cfg.threads)){usage(argv[0]);return 2;}}
        else if(!strcmp(argv[i],"--children")&&i+1<argc){
            if(parse_int_strict(argv[++i],1,MAX_CHILDREN,&cfg.children)){
                usage(argv[0]);return 2;}}
        else if(!strcmp(argv[i],"--elements")&&i+1<argc){
            unsigned long el;
            if(parse_ulong_strict(argv[++i],4096,1u<<20,&el)){
                usage(argv[0]);return 2;}
            cfg.elements=(unsigned)el;}
        else if(!strcmp(argv[i],"--deadline-ms")&&i+1<argc){
            if(parse_int_strict(argv[++i],500,60000,&cfg.deadline_ms)){
                usage(argv[0]);return 2;}}
        else if(!strcmp(argv[i],"--gpus")&&i+1<argc)gpus_arg=argv[++i];
        else{usage(argv[0]);return 2;}}
    if(child_mode){if(load_api()<0)return 2;return child_main(argc,argv);}
    /* Sentinel children == -1 ("derive from card count") is the only
     * non-strict value allowed here; explicit values were already range
     * checked during parsing. */
    if(cfg.cycles<1||cfg.cycles>MAX_ITEMS||
       cfg.thread_cycles<1||cfg.thread_cycles>MAX_ITEMS||
       cfg.child_cycles<1||cfg.child_cycles>MAX_ITEMS||
       cfg.threads<DEFAULT_THREADS||cfg.threads>MAX_THREADS||
       (cfg.children!=-1&&(cfg.children<1||cfg.children>MAX_CHILDREN))||
       cfg.elements<4096||cfg.elements>(1u<<20)||
       cfg.deadline_ms<500||cfg.deadline_ms>60000){
        usage(argv[0]);return 2;}
    if(load_api()<0)return 2;
    init_rc=API.cuInit(0);
    {struct jb b;jb_init(&b,256);
     jb_printf(&b,"{\"type\":\"meta\",\"schema\":1,\"init_rc\":%d,\"iterations\":%d,"
               "\"thread_cycles\":%d,\"child_cycles\":%d,\"threads\":%d,\"children\":%d,"
               "\"elements\":%u,\"bytes\":%u,\"pid\":%ld}",
               init_rc,cfg.cycles,cfg.thread_cycles,cfg.child_cycles,
               cfg.threads,cfg.children==-1?4:cfg.children,cfg.elements,
               cfg.elements*4u,(long)getpid());emit(&b);}
    if(init_rc){record_failuref("cuinit","rc=%d",init_rc);goto finish_failed;}
    if(API.cuDeviceGetCount(&count)||count<=0){record_failure("device_count");goto finish_failed;}
    /* Fail BEFORE copying into the fixed ordinals array if the host ever
     * reports more cards than MAX_ITEMS. */
    if(count>MAX_ITEMS){
        record_failuref("device_count","count=%d exceeds MAX_ITEMS=%d",
                        count,MAX_ITEMS);
        goto finish_failed;}
    if(gpus_arg){if(parse_ordinals(gpus_arg,&cfg,count)<0){usage(argv[0]);return 2;}}
    else{for(i=0;i<count;i++)cfg.ordinals[cfg.ngpu++]=i;}
    if(cfg.children==-1)cfg.children=cfg.ngpu;
    if(cfg.children>MAX_CHILDREN||cfg.threads>MAX_THREADS){
        usage(argv[0]);return 2;}
    if(readlink("/proc/self/exe",self,sizeof(self)-1)>0)selfp=self;
    devs=calloc((size_t)cfg.ngpu,sizeof(*devs));
    uuidp=calloc((size_t)cfg.ngpu,sizeof(*uuidp));
    if(!devs||!uuidp){record_failure("parent_calloc");goto finish_failed;}
    for(i=0;i<cfg.ngpu;i++){
        int bad=0;unsigned char bytes[16]={0};
        char name[NAMEBUFLEN]={0};
        char *uuidstr=malloc(UUID_LEN);
        size_t total=0;
        if(!uuidstr){record_failure("parent_calloc");goto finish_failed;}
        uuidp[i]=uuidstr;
        if(API.cuDeviceGet(&devs[i],cfg.ordinals[i]))bad=1;
        if(API.cuDeviceGetName(name,sizeof(name),devs[i]))bad=1;
        if(API.cuDeviceGetUuid(bytes,devs[i])){bad=1;
            snprintf(uuidstr,UUID_LEN,
                     "GPU-00000000-0000-0000-0000-0000000000%02x",
                     cfg.ordinals[i]);
            emit_inventory(cfg.ordinals[i],uuidstr,0,name,0);}
        else{uuid_format(uuidstr,bytes);
            API.cuDeviceTotalMem_v2(&total,devs[i]);
            emit_inventory(cfg.ordinals[i],uuidstr,1,name,(uint64_t)total);}
        if(bad){record_failuref("inventory_query","ordinal=%d",
                               cfg.ordinals[i]);
                atomic_store(&global_failed,1);}
    }
    begin_ns=now_ns();
    for(i=0;i<cfg.ngpu;i++)run_solo_phase(devs[i],i,cfg.ordinals[i],uuidp[i],&cfg);
    run_thread_phase(&cfg,devs,uuidp);
    run_child_phase(selfp,&cfg,uuidp);
    run_peer_phase(&cfg,devs,uuidp);
    end_ns=now_ns();
    {struct jb b;jb_init(&b,512);
     jb_printf(&b,"{\"type\":\"summary\",\"schema\":1,\"status\":\"%s\","
               "\"device_count\":%d,\"iterations\":%d,\"thread_cycles\":%d,"
               "\"child_cycles\":%d,\"threads\":%d,\"children\":%d,"
               "\"elements\":%u,\"start_ns\":%lld,\"end_ns\":%lld,\"pid\":%ld}",
               atomic_load(&global_failed)?"fail":"pass",cfg.ngpu,cfg.cycles,
               cfg.thread_cycles,cfg.child_cycles,cfg.threads,cfg.children,
               cfg.elements,(long long)begin_ns,(long long)end_ns,(long long)getpid());
     emit(&b);}
    free(devs);if(uuidp){for(i=0;i<cfg.ngpu;i++)free((void*)uuidp[i]);free(uuidp);}
    return atomic_load(&global_failed)?1:0;
finish_failed:
    cfg.ngpu=0;
    {struct jb b;jb_init(&b,256);
     jb_printf(&b,"{\"type\":\"summary\",\"schema\":1,\"status\":\"fail\","
               "\"device_count\":0,\"iterations\":%d,\"pid\":%ld}",
               cfg.cycles,(long)getpid());emit(&b);}
    free(devs);
    if(uuidp){for(i=0;i<cfg.ngpu;i++)free((void*)uuidp[i]);free(uuidp);}
    return 1;
}
