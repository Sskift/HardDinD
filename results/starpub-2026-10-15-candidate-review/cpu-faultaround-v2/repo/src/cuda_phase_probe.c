/* Real CUDA compute through the driver API. No dependency on CUDA headers. */
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>
typedef int CUresult;
typedef int CUdevice;
typedef void *CUcontext;
typedef void *CUmodule;
typedef void *CUfunction;
typedef unsigned long long CUdeviceptr;
#define DECL(name, args) static CUresult (*name) args
DECL(cuInit,(unsigned));
DECL(cuDeviceGetCount,(int *));
DECL(cuDeviceGet,(CUdevice *,int));
DECL(cuDeviceGetName,(char *,int,CUdevice));
DECL(cuCtxCreate_v2,(CUcontext *,unsigned,CUdevice));
DECL(cuCtxDestroy_v2,(CUcontext));
DECL(cuMemAlloc_v2,(CUdeviceptr *,size_t));
DECL(cuMemFree_v2,(CUdeviceptr));
DECL(cuMemcpyHtoD_v2,(CUdeviceptr,const void *,size_t));
DECL(cuMemcpyDtoH_v2,(void *,CUdeviceptr,size_t));
DECL(cuModuleLoadData,(CUmodule *,const void *));
DECL(cuModuleUnload,(CUmodule));
DECL(cuModuleGetFunction,(CUfunction *,CUmodule,const char *));
DECL(cuLaunchKernel,(CUfunction,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned,void *,void **,void **));
DECL(cuCtxSynchronize,(void));
#define LOAD(n) do { *(void **)(&n)=dlsym(lib,#n); if(!n) {fprintf(stderr,"missing %s\n",#n);return 2;} } while(0)
#define CUDA(expr) do {int e=(expr);if(e){fprintf(stderr,"%s: CUDA error %d\n",#expr,e);return 1;}}while(0)
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
static uint64_t now(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)){perror("clock_gettime");exit(2);}return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;}
int main(int argc,char **argv){
 int reverse=argc==2&&!strcmp(argv[1],"reverse");
 if(argc>2||(argc==2&&!reverse&&strcmp(argv[1],"forward")))return 2;
 void *lib=dlopen("libcuda.so.1",RTLD_NOW|RTLD_LOCAL);
 if(!lib){fprintf(stderr,"%s\n",dlerror());return 2;}
 LOAD(cuInit);LOAD(cuDeviceGetCount);LOAD(cuDeviceGet);LOAD(cuDeviceGetName);
 LOAD(cuCtxCreate_v2);LOAD(cuCtxDestroy_v2);LOAD(cuMemAlloc_v2);LOAD(cuMemFree_v2);
 LOAD(cuMemcpyHtoD_v2);LOAD(cuMemcpyDtoH_v2);LOAD(cuModuleLoadData);LOAD(cuModuleUnload);
 LOAD(cuModuleGetFunction);LOAD(cuLaunchKernel);LOAD(cuCtxSynchronize);
 CUDA(cuInit(0));int count=0;CUDA(cuDeviceGetCount(&count));if(count!=4){fprintf(stderr,"expected four GPUs, got %d\n",count);return 1;}
 unsigned n=1<<20;size_t bytes=n*sizeof(float);
 float *a=malloc(bytes),*b=malloc(bytes),*c=malloc(bytes);if(!a||!b||!c)return 2;
 for(unsigned i=0;i<n;i++){a[i]=(float)(i%1024);b[i]=(float)(i%257);}
 for(int position=0;position<count;position++){
  int ordinal=reverse?count-1-position:position;
  CUdevice dev;CUcontext ctx;CUmodule mod;CUfunction fn;CUdeviceptr da,db,dc;char name[256];
  CUDA(cuDeviceGet(&dev,ordinal));CUDA(cuDeviceGetName(name,sizeof(name),dev));
  CUDA(cuCtxCreate_v2(&ctx,0,dev));CUDA(cuModuleLoadData(&mod,ptx));CUDA(cuModuleGetFunction(&fn,mod,"add"));
  CUDA(cuMemAlloc_v2(&da,bytes));CUDA(cuMemAlloc_v2(&db,bytes));CUDA(cuMemAlloc_v2(&dc,bytes));
  for(int iteration=0;iteration<8;iteration++){
   for(unsigned i=0;i<n;i++){a[i]=(float)(i%1024+iteration);b[i]=(float)(i%257);c[i]=-1234567.0f;}
   uint64_t t[6];t[0]=now();CUDA(cuMemcpyHtoD_v2(da,a,bytes));t[1]=now();
   CUDA(cuMemcpyHtoD_v2(db,b,bytes));t[2]=now();void *args[]={&da,&db,&dc,&n};
   CUDA(cuLaunchKernel(fn,(n+255)/256,1,1,256,1,1,0,NULL,args,NULL));t[3]=now();
   CUDA(cuCtxSynchronize());t[4]=now();CUDA(cuMemcpyDtoH_v2(c,dc,bytes));t[5]=now();
   for(int i=1;i<6;i++)if(t[i]<t[i-1]){fprintf(stderr,"clock reversed\n");return 1;}
   for(unsigned i=0;i<n;i++)if(c[i]!=a[i]+b[i]){fprintf(stderr,"GPU %d iteration %d mismatch at %u\n",ordinal,iteration,i);return 1;}
   printf("{\"gpu\":%d,\"position\":%d,\"order\":\"%s\",\"iteration\":%d,\"warmup\":%s,\"elements\":%u,\"verified\":true,\"start_ns\":%llu,\"htod_a_ns\":%llu,\"htod_b_ns\":%llu,\"launch_ns\":%llu,\"sync_ns\":%llu,\"dtoh_ns\":%llu,\"total_ns\":%llu}\n",ordinal,position,reverse?"reverse":"forward",iteration,iteration<2?"true":"false",n,(unsigned long long)t[0],(unsigned long long)(t[1]-t[0]),(unsigned long long)(t[2]-t[1]),(unsigned long long)(t[3]-t[2]),(unsigned long long)(t[4]-t[3]),(unsigned long long)(t[5]-t[4]),(unsigned long long)(t[5]-t[0]));fflush(stdout);
  }
  CUDA(cuMemFree_v2(da));CUDA(cuMemFree_v2(db));CUDA(cuMemFree_v2(dc));CUDA(cuModuleUnload(mod));CUDA(cuCtxDestroy_v2(ctx));
 }
 free(a);free(b);free(c);dlclose(lib);return 0;
}
