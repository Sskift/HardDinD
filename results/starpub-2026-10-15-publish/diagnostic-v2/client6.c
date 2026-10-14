#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
int main(int argc,char **argv){
 if(argc!=3)return 2;
 int fd=socket(AF_INET6,SOCK_STREAM,0); if(fd<0){perror("socket");return 1;}
 struct timeval tv={3,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof(tv));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&tv,sizeof(tv));
 struct sockaddr_in6 a={.sin6_family=AF_INET6,.sin6_port=htons(atoi(argv[2]))};if(inet_pton(AF_INET6,argv[1],&a.sin6_addr)!=1)return 2;
 if(connect(fd,(struct sockaddr*)&a,sizeof(a))){perror("connect");return 1;}puts("connected");fflush(stdout);
 if(send(fd,"ok",2,0)!=2){perror("send");return 1;}char x[2];ssize_t got=recv(fd,x,2,MSG_WAITALL);if(got!=2||memcmp(x,"ok",2)){perror("recv/verify");return 1;}puts("echo_verified");close(fd);return 0;
}
