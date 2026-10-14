/* Fixed NVIDIA 550.120 RM_CONTROL root queries, not CUDA acceptance.
 * nvos.h:2208-2216; ctrl0000gpu.h:66,162,325,628.
 * Native and guest runners compare the raw error results as well as checks. */
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
struct ids { uint32_t id[32]; };
struct info { uint32_t id,flags,device,subdevice,sli,board,instance; int32_t numa; };
struct pci { uint32_t id,domain; uint16_t bus,slot; };
struct uuid_info { uint32_t id,flags; unsigned char uuid[256]; uint32_t length; };
_Static_assert(sizeof(struct alloc)==48, "NVOS64");
_Static_assert(sizeof(struct release)==16, "NVOS00");
_Static_assert(sizeof(struct control)==32 && offsetof(struct control,params)==16
               && offsetof(struct control,size)==24, "NVOS54");
_Static_assert(sizeof(struct ids)==128 && sizeof(struct info)==32
               && sizeof(struct pci)==12 && sizeof(struct uuid_info)==268, "root controls");
#define ALLOC _IOWR('F',0x2b,struct alloc)
#define FREE _IOWR('F',0x29,struct release)
#define CONTROL _IOWR('F',0x2a,struct control)
static int failures;
static const char *prefix;
static void check(const char *name,int ok) {
    printf("GPU_CTRL_CHECK %s %s\n",name,ok?"pass":"fail");
    if (!ok) failures=1;
}
static int open_control(void) {
    char p[256];snprintf(p,sizeof(p),"%sctl",prefix);int fd=open(p,O_RDWR|O_CLOEXEC);
    if(fd<0)perror(p);
    return fd;
}
static uint32_t allocate(int fd) {
    struct alloc p={0};
    if(ioctl(fd,ALLOC,&p)||p.status||!p.object)return 0;
    return p.object;
}
static int release(int fd,uint32_t client) {
    struct release p={client,client,client,0};
    return ioctl(fd,FREE,&p)==0 && p.status==0;
}
struct response { int rc,error; uint32_t status; int pointer_unchanged; };
static struct response query(int fd,uint32_t client,uint32_t cmd,void *data,uint32_t size) {
    struct control p={client,client,cmd,0,(uintptr_t)data,size,0};
    errno=0;int rc=ioctl(fd,CONTROL,&p),e=errno;
    return (struct response){rc,e,p.status,p.params==(uintptr_t)data};
}
static int okay(struct response r) { return r.rc==0 && r.status==0 && r.pointer_unchanged; }
static void record(const char *name,struct response r) {
    printf("GPU_CTRL_RESULT {\"case\":\"%s\",\"rc\":%d,\"errno\":%d,\"status\":%u,\"pointer_unchanged\":%s}\n",
           name,r.rc,r.error,r.status,r.pointer_unchanged?"true":"false");
}
struct thread { int fd;uint32_t client;struct ids expected;int okay; };
static void *thread_query(void *arg) {
    struct thread *t=arg;t->okay=1;
    for(int i=0;i<16;i++) {
        struct ids ids={0};struct response r=query(t->fd,t->client,0x201,&ids,sizeof(ids));
        if(!okay(r)||memcmp(&ids,&t->expected,sizeof(ids)))t->okay=0;
    }
    return NULL;
}
int main(int argc,char **argv) {
    if(argc!=3)return 2;
    prefix=argv[1];int count=atoi(argv[2]);
    if(count<1||count>32)return 2;
    setvbuf(stdout,NULL,_IOLBF,0);
    int fd=open_control();check("open_control",fd>=0);if(fd<0)return 1;
    uint32_t client=allocate(fd);check("allocate_client",client!=0);
    if(!client){close(fd);return 1;}
    struct ids ids;memset(&ids,0,sizeof(ids));
    struct response r=query(fd,client,0x201,&ids,sizeof(ids));record("attached_ids",r);
    int good=okay(r);
    for(int i=0;i<32;i++) {
        if((i<count)==(ids.id[i]==UINT32_MAX))good=0;
        for(int j=0;j<i&&i<count;j++)if(ids.id[i]==ids.id[j])good=0;
    }
    check("attached_ids",good);
    for(int i=0;i<count;i++) {
        struct info info={.id=ids.id[i]};
        struct pci pci={.id=ids.id[i]};
        struct uuid_info uuid={.id=ids.id[i]};
        struct response a=query(fd,client,0x205,&info,sizeof(info));
        struct response b=query(fd,client,0x21b,&pci,sizeof(pci));
        struct response c=query(fd,client,0x275,&uuid,sizeof(uuid));
        char name[64];snprintf(name,sizeof(name),"gpu_%d_details",i);
        int valid=okay(a)&&okay(b)&&okay(c)&&info.id==ids.id[i]&&pci.id==ids.id[i]
                  &&uuid.id==ids.id[i]&&uuid.length>0&&uuid.length<=sizeof(uuid.uuid)
                  &&memchr(uuid.uuid,0,sizeof(uuid.uuid))&&memcmp(uuid.uuid,"GPU-",4)==0;
        check(name,valid);
        if(valid)printf("GPU_CTRL_DEVICE {\"gpu\":%d,\"id\":%u,\"device\":%u,\"subdevice\":%u,\"instance\":%u,"
                        "\"domain\":%u,\"bus\":%u,\"slot\":%u,\"uuid\":\"%s\"}\n",
                        i,ids.id[i],info.device,info.subdevice,info.instance,pci.domain,pci.bus,pci.slot,uuid.uuid);
        else {record("info_failure",a);record("pci_failure",b);record("uuid_failure",c);}
    }
    struct info bad={.id=UINT32_MAX};
    r=query(fd,client,0x205,&bad,sizeof(bad));record("invalid_gpu",r);
    check("invalid_gpu_rejected",r.rc!=0||r.status!=0);
    struct ids unused={0};
    r=query(fd,0,0x201,&unused,sizeof(unused));record("invalid_client",r);
    check("invalid_client_rejected",r.rc!=0||r.status!=0);
    size_t page=(size_t)sysconf(_SC_PAGESIZE);
    unsigned char *mem=mmap(NULL,2*page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(mem==MAP_FAILED)return 2;
    struct ids *cross=(void *)(mem+page-64);memset(cross,0,sizeof(*cross));
    r=query(fd,client,0x201,cross,sizeof(*cross));record("cross_page",r);
    check("nested_cross_page",okay(r)&&!memcmp(cross,&ids,sizeof(ids)));
    if(mprotect(mem,2*page,PROT_NONE))return 2;
    r=query(fd,client,0x201,cross,sizeof(*cross));record("nested_unreadable",r);
    check("nested_unreadable_rejected",r.rc!=0||r.status!=0);
    if(mprotect(mem,2*page,PROT_READ))return 2;
    r=query(fd,client,0x201,cross,sizeof(*cross));record("nested_readonly",r);
    check("nested_readonly_rejected",r.rc!=0||r.status!=0);
    if(mprotect(mem,2*page,PROT_READ|PROT_WRITE))return 2;
    struct control *header=(void*)mem;
    *header=(struct control){client,client,0x201,0,(uintptr_t)&unused,sizeof(unused),0};
    if(mprotect(mem,page,PROT_READ))return 2;
    errno=0;int rc=ioctl(fd,CONTROL,header),error=errno;
    record("header_readonly",(struct response){rc,error,header->status,header->params==(uintptr_t)&unused});
    check("header_readonly_rejected",rc==-1&&error==EFAULT);
    if(mprotect(mem,2*page,PROT_READ|PROT_WRITE))return 2;
    munmap(mem,2*page);
    r=query(fd,client,0x201,&unused,sizeof(unused));record("recovery",r);
    check("recovery_after_faults",okay(r)&&!memcmp(&unused,&ids,sizeof(ids)));
    /* Boundary faults must preserve the driver's status/errno contract too. */
    struct { const char *name; void *pointer; } edges[] = {
        {"nested_null", NULL}, {"nested_low_unmapped", (void *)(uintptr_t)1},
        {"nested_wrap", (void *)UINTPTR_MAX},
        {"nested_noncanonical", (void *)(uintptr_t)(1ULL << 63)},
        {"nested_unmapped", mem},
    };
    for (unsigned i=0;i<sizeof(edges)/sizeof(edges[0]);i++) {
        r=query(fd,client,0x201,edges[i].pointer,sizeof(struct ids));record(edges[i].name,r);
        check(edges[i].name,r.rc!=0||r.status!=0);
    }
    pthread_t th[4];struct thread t[4];
    for(int i=0;i<4;i++) {
        t[i]=(struct thread){fd,client,ids,0};
        if(pthread_create(&th[i],NULL,thread_query,&t[i]))return 2;
    }
    good=1;
    for(int i=0;i<4;i++)if(pthread_join(th[i],NULL)||!t[i].okay)good=0;
    check("four_threads_same_client",good);
    good=1;
    for(int i=0;i<4;i++) {
        pid_t pid=fork();if(pid<0)return 2;
        if(!pid) {
            close(fd);int other=open_control();uint32_t c=allocate(other);struct ids child={0};
            struct response q=query(other,c,0x201,&child,sizeof(child));
            int ok=c&&okay(q)&&!memcmp(&child,&ids,sizeof(ids))&&release(other,c);
            close(other);_exit(ok?0:1);
        }
        int status;if(waitpid(pid,&status,0)!=pid||!WIFEXITED(status)||WEXITSTATUS(status))good=0;
    }
    check("independent_process_queries",good);
    check("release_client",release(fd,client));close(fd);
    printf("GPU_CTRL_END status=%d internal_cuda_verified=0\n",failures);return failures;
}
