/* NVIDIA 550.120 legacy GET_BUILD_VERSION pointer contract, not CUDA acceptance. */
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
struct build { uint32_t size,pad; uint64_t pointer[3]; uint32_t change,official; };
_Static_assert(sizeof(struct alloc)==48 && sizeof(struct release)==16, "RM root ABI");
_Static_assert(sizeof(struct control)==32 && offsetof(struct control,params)==16, "NVOS54");
_Static_assert(sizeof(struct build)==40 && offsetof(struct build,pointer)==8 && offsetof(struct build,change)==32, "BUILD_VERSION");
#define ALLOC _IOWR('F',0x2b,struct alloc)
#define FREE _IOWR('F',0x29,struct release)
#define CONTROL _IOWR('F',0x2a,struct control)
static int failures;
static const char *prefix;
static void check(const char *name,int ok) { printf("GPU_BUILD_CHECK %s %s\n",name,ok?"pass":"fail"); if(!ok) failures=1; }
static int open_control(void) { char path[256];snprintf(path,sizeof(path),"%sctl",prefix);int fd=open(path,O_RDWR|O_CLOEXEC);if(fd<0)perror(path);return fd; }
static uint32_t allocate(int fd) { struct alloc p={0};return ioctl(fd,ALLOC,&p)==0 && !p.status ? p.object:0; }
static int release(int fd,uint32_t client) { struct release p={client,client,client,0};return !ioctl(fd,FREE,&p)&&!p.status; }
static int query(int fd,uint32_t client,struct build *p) {
    struct control c={client,client,0x101,0,(uintptr_t)p,sizeof(*p),0};
    return ioctl(fd,CONTROL,&c)==0 && c.status==0 && c.params==(uintptr_t)p;
}
static int full_query(int fd,uint32_t client) {
    char s[3][256];memset(s,0xa5,sizeof(s));
    struct build b={.size=256,.pointer={(uintptr_t)s[0],(uintptr_t)s[1],(uintptr_t)s[2]}};
    if(!query(fd,client,&b))return 0;
    for(int i=0;i<3;i++)if(!s[i][0]||!memchr(s[i],0,sizeof(s[i])))return 0;
    return 1;
}
struct thread { int fd,okay;uint32_t client; };
static void *thread_query(void *arg) {
    struct thread *t=arg;t->okay=1;
    for(int i=0;i<8;i++)if(!full_query(t->fd,t->client))t->okay=0;
    return NULL;
}
static void hex(const unsigned char *p,size_t n) { for(size_t i=0;i<n;i++)printf("%02x",p[i]); }
static void run_case(int fd,uint32_t client,int which,const char *name) {
    size_t page=(size_t)sysconf(_SC_PAGESIZE);
    unsigned char *mem=mmap(NULL,10*page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(mem==MAP_FAILED)exit(2);
    memset(mem,0xa5,10*page);
    struct control *c=(void*)mem;
    struct build *b=(void*)(mem+page);
    unsigned char *leaf[3]={mem+3*page,mem+5*page,mem+7*page};
    if(which==18 || which==19)b=(void*)(mem+2*page-20);
    if(which==9 || which==10)leaf[1]=mem+6*page-4;
    *b=(struct build){.size=256,.pad=0x5a5a5a5a,.pointer={(uintptr_t)leaf[0],(uintptr_t)leaf[1],(uintptr_t)leaf[2]},.change=0x12345678,.official=0x87654321};
    if(which==0 || which==12)memset(b->pointer,0,sizeof(b->pointer));
    if(which==2)b->size=1;
    if(which==3){b->pointer[0]=1;b->pointer[1]=0;b->pointer[2]=1ULL<<63;}
    if(which==6)b->pointer[2]=1;
    if(which==7)b->pointer[0]=1ULL<<63;
    if(which==8)b->pointer[2]=UINT64_MAX;
    *c=(struct control){client,client,0x101,0,(uintptr_t)b,sizeof(*b),0xfeedbeef};
    if(which==14)c->params=1;
    if(which==15)c->params=0;
    if(which==16 || which==17)c->params=1ULL<<63;
    if(which==17)c->client=c->object=0;
    if(which==22)c->size=39;
    if(which==23)c->size=41;
    uint64_t original_child=c->params,original_leaf[3];memcpy(original_leaf,b->pointer,sizeof(original_leaf));
    if(which==4 && mprotect(mem+3*page,page,PROT_READ))exit(2);
    if(which==5 && mprotect(mem+5*page,page,PROT_NONE))exit(2);
    if(which==10 && mprotect(mem+6*page,page,PROT_NONE))exit(2);
    if((which==11||which==12) && mprotect(mem+page,page,PROT_READ))exit(2);
    if(which==13 && mprotect(mem+page,page,PROT_NONE))exit(2);
    if(which==19 && mprotect(mem+2*page,page,PROT_READ))exit(2);
    if(which==20 && mprotect(mem,page,PROT_READ))exit(2);
    if(which==21 && mprotect(mem,page,PROT_NONE))exit(2);
    errno=0;int rc=ioctl(fd,CONTROL,c),err=errno;
    if(mprotect(mem,10*page,PROT_READ|PROT_WRITE))exit(2);
    printf("GPU_BUILD_RESULT {\"case\":\"%s\",\"rc\":%d,\"errno\":%d,\"status\":%u,\"header_pointer_unchanged\":%s,\"leaf_pointers_unchanged\":%s,\"size\":%u,\"pad\":%u,\"change\":%u,\"official\":%u,\"strings_hex\":[",name,rc,err,c->status,c->params==original_child?"true":"false",!memcmp(b->pointer,original_leaf,sizeof(original_leaf))?"true":"false",b->size,b->pad,b->change,b->official);
    for(int i=0;i<3;i++){printf("%s\"",i?",":"");hex(leaf[i],256);printf("\"");}puts("]}");
    /* Exact statuses and partial writes are compared against native by the runner. */
    int success=which==0||which==1||which==3||which==9||which==11||which==12||which==18||which==19||which==22||which==23;
    check(name,(success ? (rc==0&&c->status==0):(rc!=0||c->status!=0)) && c->params==original_child && !memcmp(b->pointer,original_leaf,sizeof(original_leaf)));
    munmap(mem,10*page);
}
int main(int argc,char **argv) {
    if(argc!=3||atoi(argv[2])<1)return 2;
    prefix=argv[1];setvbuf(stdout,NULL,_IOLBF,0);
    int fd=open_control();check("open_control",fd>=0);if(fd<0)return 1;
    uint32_t client=allocate(fd);check("allocate_client",client!=0);if(!client){close(fd);return 1;}
    const char *names[]={"size_query","full","too_small","partial_null","leaf_readonly","leaf_unreadable","leaf_low_unmapped","leaf_noncanonical","leaf_wrap","leaf_cross_page","leaf_cross_none","child_readonly","child_readonly_size_query","child_unreadable","child_low_unmapped","child_null","child_noncanonical","invalid_client_bad_child","child_cross_page","child_cross_readonly","header_readonly","header_unreadable","size_short","size_long"};
    for(unsigned i=0;i<sizeof(names)/sizeof(names[0]);i++)run_case(fd,client,(int)i,names[i]);
    check("recovery_after_faults",full_query(fd,client));
    pthread_t th[4];struct thread t[4];int okay=1;
    for(int i=0;i<4;i++){t[i]=(struct thread){fd,0,client};if(pthread_create(&th[i],NULL,thread_query,&t[i]))return 2;}
    for(int i=0;i<4;i++)if(pthread_join(th[i],NULL)||!t[i].okay)okay=0;
    check("four_threads_same_client",okay);okay=1;
    for(int i=0;i<4;i++){
        pid_t pid=fork();if(pid<0)return 2;
        if(!pid){close(fd);int other=open_control();uint32_t c=allocate(other);int good=c&&full_query(other,c)&&release(other,c);close(other);_exit(good?0:1);}
        int status;if(waitpid(pid,&status,0)!=pid||!WIFEXITED(status)||WEXITSTATUS(status))okay=0;
    }
    check("independent_process_queries",okay);
    check("release_client",release(fd,client));close(fd);
    printf("GPU_BUILD_END status=%d internal_cuda_verified=0\n",failures);return failures;
}
