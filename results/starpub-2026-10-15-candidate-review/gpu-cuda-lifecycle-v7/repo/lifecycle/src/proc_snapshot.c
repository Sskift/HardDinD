#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
static void quoted(const char *s) {
 putchar('"'); for (; *s; s++) { unsigned char c=(unsigned char)*s;
 if (c=='"'||c=='\\') { putchar('\\');putchar(c); }
 else if(c<32) printf("\\u%04x",c); else putchar(c); } putchar('"');
}
int main(int argc,char **argv) {
 if(argc!=2)return 2;
 char *end; long pid=strtol(argv[1],&end,10);if(*end||pid<1||pid>2147483647L)return 2;
 char path[256];snprintf(path,sizeof(path),"/proc/%ld/task",pid);DIR *d=opendir(path);
 if(!d){perror(path);return 1;}struct dirent *e;unsigned count=0;
 const char *fields[]={"comm","wchan","status","stack","syscall"};
 while((e=readdir(d)) && count<512) {
  long tid=strtol(e->d_name,&end,10);if(*end||tid<1)continue;count++;
  for(unsigned i=0;i<sizeof(fields)/sizeof(*fields);i++) {
   snprintf(path,sizeof(path),"/proc/%ld/task/%ld/%s",pid,tid,fields[i]);
   struct timespec ts;clock_gettime(CLOCK_MONOTONIC,&ts);
   printf("{\"pid\":%ld,\"tid\":%ld,\"field\":",pid,tid);quoted(fields[i]);
   printf(",\"at_ns\":%lld",(long long)ts.tv_sec*1000000000LL+ts.tv_nsec);fflush(stdout);
   int fd=open(path,O_RDONLY|O_CLOEXEC|O_NONBLOCK),err=fd<0?errno:0;char buf[16385];ssize_t n=fd<0?-1:read(fd,buf,sizeof(buf)-1);
   if(n<0&&!err)err=errno;if(fd>=0)close(fd);
   printf(",\"errno\":%d,\"data\":",err);buf[n<0?0:n]=0;quoted(buf);puts("}");fflush(stdout);
  }
 }
 closedir(d);return count==512?3:0;
}
