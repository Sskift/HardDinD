/* Fixed NVIDIA 550.120 SYSTEM_GET_FEATURES control, not CUDA acceptance. */
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
struct alloc { uint32_t root,parent,object,cls; uint64_t params,rights; uint32_t size,flags,status; };
struct release { uint32_t root,parent,object,status; };
struct control { uint32_t client,object,cmd,flags; uint64_t params; uint32_t size,status; };
_Static_assert(sizeof(struct alloc)==48 && sizeof(struct release)==16, "RM roots");
_Static_assert(sizeof(struct control)==32 && offsetof(struct control,params)==16 && offsetof(struct control,status)==28, "NVOS54");
#define ALLOC _IOWR('F',0x2b,struct alloc)
#define FREE _IOWR('F',0x29,struct release)
#define CONTROL _IOWR('F',0x2a,struct control)
static const char *prefix;
static int failed;
static size_t page;
static void check(const char *n,int ok) { printf("GPU_FEATURE_CHECK %s %s\n",n,ok?"pass":"fail");failed|=!ok; }
static void protect(void *p,size_t n,int prot) { if(mprotect(p,n,prot)){perror("mprotect");exit(2);} }
static void hex(char *out,const void *data,size_t n) { const unsigned char *p=data;for(size_t i=0;i<n;i++)sprintf(out+2*i,"%02x",p[i]);out[2*n]=0; }
static int open_ctl(void) { char p[256];snprintf(p,sizeof(p),"%sctl",prefix);int fd=open(p,O_RDWR|O_CLOEXEC);if(fd<0)perror(p);return fd; }
static uint32_t client(int fd) { struct alloc a={0};return ioctl(fd,ALLOC,&a)==0&&!a.status?a.object:0; }
static int release_client(int fd,uint32_t c) { struct release r={c,c,c,0};return ioctl(fd,FREE,&r)==0&&!r.status; }
static int query(int fd,uint32_t c,uint32_t *value) {
    struct control h={c,c,0x1f0,0,(uintptr_t)value,4,0xdeadbeef};
    return ioctl(fd,CONTROL,&h)==0&&h.status==0&&h.params==(uintptr_t)value;
}
static void one(int fd,uint32_t c,const char *name) {
    unsigned char *m=mmap(NULL,4*page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(m==MAP_FAILED){perror("mmap");exit(2);}
    struct control *h=(void*)m;void *child=m+page;
    uint32_t value=0xa5b6c7d8;int readable=1;
    if(!strcmp(name,"zeroed"))value=0;
    if(!strncmp(name,"cross_",6))child=m+2*page-2;
    memcpy(child,&value,4);
    *h=(struct control){c,c,0x1f0,0,(uintptr_t)child,4,0xdeadbeef};
    if(!strcmp(name,"client_zero")||!strcmp(name,"client_zero_bad_size")||!strcmp(name,"client_zero_bad_pointer"))h->client=0;
    if(!strcmp(name,"object_zero"))h->object=0;
    if(!strcmp(name,"object_unknown"))h->object=0xfedcba98;
    if(!strcmp(name,"child_null")||!strcmp(name,"null_size_zero"))h->params=0,readable=0;
    if(!strcmp(name,"child_low")||!strcmp(name,"client_zero_bad_pointer"))h->params=1,readable=0;
    if(!strcmp(name,"child_noncanonical"))h->params=1ull<<63,readable=0;
    if(!strcmp(name,"child_wrap"))h->params=UINT64_MAX,readable=0;
    if(!strcmp(name,"size_zero")||!strcmp(name,"null_size_zero"))h->size=0;
    if(!strcmp(name,"size_short")||!strcmp(name,"client_zero_bad_size"))h->size=3;
    if(!strcmp(name,"size_long"))h->size=5;
    if(!strcmp(name,"size_max"))h->size=UINT32_MAX;
    if(!strcmp(name,"flags_1"))h->flags=1;
    if(!strcmp(name,"flags_2"))h->flags=2;
    if(!strcmp(name,"flags_4")||!strcmp(name,"finn_readonly")||!strcmp(name,"finn_bad_size"))h->flags=4;
    if(!strcmp(name,"flags_8"))h->flags=8;
    if(!strcmp(name,"flags_high"))h->flags=0x80000000;
    if(!strcmp(name,"flags_all"))h->flags=UINT32_MAX;
    if(!strcmp(name,"finn_bad_size"))h->size=UINT32_MAX;
    char hb[65],ha[65],cb[9],ca[9];hex(hb,h,32);hex(cb,child,4);
    if(!readable)strcpy(cb,"n/a");
    if(!strcmp(name,"child_readonly")||!strcmp(name,"finn_readonly"))protect(m+page,page,PROT_READ);
    if(!strcmp(name,"child_none"))protect(m+page,page,PROT_NONE);
    if(!strcmp(name,"cross_ro"))protect(m+2*page,page,PROT_READ);
    if(!strcmp(name,"cross_none"))protect(m+2*page,page,PROT_NONE);
    if(!strcmp(name,"header_readonly"))protect(m,page,PROT_READ);
    if(!strcmp(name,"header_none"))protect(m,page,PROT_NONE);
    errno=0;int rc=ioctl(fd,CONTROL,h),err=errno;
    protect(m,4*page,PROT_READ|PROT_WRITE);hex(ha,h,32);
    if(readable)hex(ca,child,4);else strcpy(ca,"n/a");
    printf("GPU_FEATURE_RESULT {\"case\":\"%s\",\"rc\":%d,\"errno\":%d,\"header_before\":\"%s\",\"header_after\":\"%s\",\"child_before\":\"%s\",\"child_after\":\"%s\"}\n",name,rc,err,hb,ha,cb,ca);
    if(!strcmp(name,"normal")||!strcmp(name,"zeroed"))check(name,rc==0&&h->status==0&&h->params==(uintptr_t)child);
    munmap(m,4*page);
}
struct loop { int fd,count;uint32_t client,expected; };
static void *loop(void *arg) {
    struct loop *p=arg;
    for(int i=0;i<p->count;i++){uint32_t v=0xa5a5a5a5;if(!query(p->fd,p->client,&v)||v!=p->expected)return (void*)1;}
    return NULL;
}
int main(int argc,char **argv) {
    if(argc!=3)return 2;
    int count=atoi(argv[2]);
    if(count<1||count>1000)return 2;
    prefix=argv[1];page=(size_t)sysconf(_SC_PAGESIZE);setvbuf(stdout,NULL,_IOLBF,0);
    int fd=open_ctl();check("open_control",fd>=0);if(fd<0)return 1;
    uint32_t c=client(fd);check("client",c!=0);if(!c){close(fd);return 1;}
    printf("GPU_FEATURE_IDENTITIES {\"client\":%u}\n",c);
    uint32_t expected=UINT32_MAX;check("initial_query",query(fd,c,&expected));
    static const char *names[]={"normal","zeroed","client_zero","object_zero","object_unknown", "child_null",
        "child_low","child_noncanonical","child_wrap","child_readonly","child_none","cross_rw","cross_ro","cross_none",
        "header_readonly","header_none","size_zero","size_short","size_long","size_max","null_size_zero",
        "flags_1","flags_2","flags_4","flags_8","flags_high","flags_all","client_zero_bad_size",
        "client_zero_bad_pointer","finn_readonly","finn_bad_size"};
    for(size_t i=0;i<sizeof(names)/sizeof(*names);i++)one(fd,c,names[i]);
    pthread_t ts[4];struct loop args[4];int started=0,ok=1;
    for(int i=0;i<4;i++){args[i]=(struct loop){fd,count,c,expected};if(pthread_create(&ts[i],NULL,loop,&args[i])){ok=0;break;}started++;}
    for(int i=0;i<started;i++){void *rc;if(pthread_join(ts[i],&rc)||rc)ok=0;}
    check("four_threads",ok);check("release_client",release_client(fd,c));close(fd);
    for(int i=0;i<4;i++){
        pid_t pid=fork();if(pid==0){int f=open_ctl();uint32_t cc=f<0?0:client(f);struct loop l={f,count,cc,expected};int good=cc&&!loop(&l);if(cc)good&=release_client(f,cc);if(f>=0)close(f);_exit(good?0:1);}
        int status=-1;char name[40];snprintf(name,sizeof(name),"independent_process_%d",i);
        check(name,pid>0&&waitpid(pid,&status,0)==pid&&WIFEXITED(status)&&WEXITSTATUS(status)==0);
    }
    printf("GPU_FEATURE_END status=%d internal_cuda_verified=0\n",failed);return failed;
}
