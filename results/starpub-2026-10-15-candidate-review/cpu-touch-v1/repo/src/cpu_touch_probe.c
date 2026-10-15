#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
static uint64_t now(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)){perror("clock");exit(2);}return (uint64_t)t.tv_sec*1000000000ULL+t.tv_nsec;}
static void usage(struct rusage *r){if(getrusage(RUSAGE_SELF,r)){perror("getrusage");exit(2);}}
int main(void){
 const size_t sizes[]={1048576,4194304,16777216};long page=sysconf(_SC_PAGESIZE);if(page<=0)return 2;
 for(unsigned sz=0;sz<sizeof(sizes)/sizeof(sizes[0]);sz++)for(unsigned rep=0;rep<8;rep++){
  size_t bytes=sizes[sz];if(bytes%(size_t)page)return 2;
  volatile unsigned char *p=mmap(NULL,bytes,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);if(p==MAP_FAILED){perror("mmap");return 1;}
  struct rusage a={0},b={0},c={0};usage(&a);
  uint64_t t0=now();for(size_t i=0;i<bytes;i+=(size_t)page)p[i]=(unsigned char)(i/(size_t)page+rep);uint64_t t1=now();usage(&b);
  for(size_t i=0;i<bytes;i+=(size_t)page)if(p[i]!=(unsigned char)(i/(size_t)page+rep))return 1;
  uint64_t t2=now();for(size_t i=0;i<bytes;i+=(size_t)page)p[i]=(unsigned char)(i/(size_t)page+rep+1);uint64_t t3=now();usage(&c);
  for(size_t i=0;i<bytes;i+=(size_t)page)if(p[i]!=(unsigned char)(i/(size_t)page+rep+1))return 1;
  if(t1<t0||t3<t2||b.ru_minflt<a.ru_minflt||c.ru_minflt<b.ru_minflt||b.ru_majflt<a.ru_majflt||c.ru_majflt<b.ru_majflt)return 1;
  if(munmap((void *)p,bytes)){perror("munmap");return 1;}
  printf("{\"bytes\":%zu,\"page_size\":%ld,\"pages\":%zu,\"iteration\":%u,\"cold_ns\":%llu,\"warm_ns\":%llu,\"cold_minor\":%ld,\"warm_minor\":%ld,\"cold_major\":%ld,\"warm_major\":%ld,\"verified\":true}\n",bytes,page,bytes/(size_t)page,rep,(unsigned long long)(t1-t0),(unsigned long long)(t3-t2),b.ru_minflt-a.ru_minflt,c.ru_minflt-b.ru_minflt,b.ru_majflt-a.ru_majflt,c.ru_majflt-b.ru_majflt);fflush(stdout);
 }
 return 0;
}
