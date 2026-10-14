/* Trace real NVIDIA ioctl headers without replacing their implementation.
 * Diagnostic preload only: timings under this logger are not benchmarks. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>

static size_t snapshot(void *arg, unsigned char *bytes, size_t limit) {
    struct iovec local = {bytes, limit}, remote = {arg, limit};
    long got = syscall(SYS_process_vm_readv, getpid(), &local, 1, &remote, 1, 0);
    return got > 0 ? (size_t)got : 0;
}
static void hex(char *out, const unsigned char *bytes, size_t size) {
    static const char chars[]="0123456789abcdef";
    for(size_t i=0;i<size;i++){out[2*i]=chars[bytes[i]>>4];out[2*i+1]=chars[bytes[i]&15];}
    out[2*size]=0;
}
int ioctl(int fd, unsigned long command, ...) {
    va_list args;va_start(args,command);void *arg=va_arg(args,void*);va_end(args);
    int saved_errno=errno;
    char link[64],device[256];snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);
    long length=syscall(SYS_readlink,link,device,sizeof(device)-1);
    if(length<0)length=0;
    device[length]=0;
    int trace=!strncmp(device,"/dev/nvidia",11)||!strncmp(device,"/dev/harddind-nvidia",20);
    unsigned char before[64],after[64];size_t a=0,b=0;
    /* UVM uses unencoded request sizes; do not read beyond an unknown struct. */
    size_t limit=_IOC_TYPE(command)=='F'?_IOC_SIZE(command):0;
    if(limit>sizeof(before))limit=sizeof(before);
    if(trace&&arg&&limit)a=snapshot(arg,before,limit);
    errno=saved_errno;
    int result=(int)syscall(SYS_ioctl,fd,command,arg),result_errno=errno;
    if(trace){
        if(arg&&limit)b=snapshot(arg,after,limit);
        char ah[129],bh[129],line[1024];hex(ah,before,a);hex(bh,after,b);
        int size=snprintf(line,sizeof(line),"CUDA_IOCTL pid=%ld tid=%ld fd=%d device=%s request=0x%lx before=%s after=%s rc=%d errno=%d\n",
            (long)getpid(),(long)syscall(SYS_gettid),fd,device,command,ah,bh,result,result_errno);
        if(size>0&&(size_t)size<sizeof(line))syscall(SYS_write,STDERR_FILENO,line,(size_t)size);
    }
    errno=result_errno;return result;
}
