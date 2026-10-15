#include "common.h"
#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>

#define BLOCK 65536
#define MAGIC 0x48444231U
static unsigned char pattern[BLOCK];
static void socket_options(int fd) {
    int one = 1; struct timeval limit = {.tv_sec = 20};
    require(setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)) == 0, "TCP_NODELAY");
    require(setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit)) == 0, "receive timeout");
    require(setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit)) == 0, "send timeout");
}
static struct sockaddr_in address(const char *host, int port) {
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    require(inet_pton(AF_INET, host, &addr.sin_addr) == 1, "IPv4 address"); return addr;
}
static void serve(int fd) {
    socket_options(fd);
    uint32_t header[2]; read_all(fd, header, sizeof(header));
    require(header[0] == MAGIC && header[1] <= 1, "protocol header");
    uint64_t count = 0; unsigned char buffer[BLOCK];
    if (header[1]) {
        for (;;) {
            ssize_t n = recv(fd, buffer, 64, MSG_WAITALL);
            if (!n) break;
            if (n < 0 && errno == EINTR) continue;
            require(n == 64 && memcmp(buffer, pattern, 64) == 0, "rr request");
            write_all(fd, buffer, 64); count++;
        }
    } else {
        for (;;) {
            ssize_t n = read(fd, buffer, sizeof(buffer));
            if (!n) break;
            if (n < 0 && errno == EINTR) continue;
            require(n > 0, "stream receive");
            size_t first = BLOCK - (size_t)(count % BLOCK);
            if (first > (size_t)n) first = (size_t)n;
            require(memcmp(buffer, pattern + count % BLOCK, first) == 0, "stream data");
            if ((size_t)n > first) require(memcmp(buffer + first, pattern, (size_t)n - first) == 0, "stream wrap");
            count += (uint64_t)n;
        }
    }
    require(count > 0, "empty transfer"); write_all(fd, &count, sizeof(count));
}
int main(int argc, char **argv) {
    require(argc >= 4, "usage: network server IP PORT | client IP PORT stream|rr SECONDS");
    for (size_t i = 0; i < BLOCK; i++) pattern[i] = (unsigned char)((i * 131 + i / 251) % 256);
    int port = atoi(argv[3]); require(port >= 0 && port <= 65535, "port");
    struct sockaddr_in addr = address(argv[2], port);
    int fd = socket(AF_INET, SOCK_STREAM, 0); require(fd >= 0, "socket");
    if (!strcmp(argv[1], "server")) {
        int one = 1; require(setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) == 0, "reuseaddr");
        require(bind(fd, (void *)&addr, sizeof(addr)) == 0 && listen(fd, 8) == 0, "listen");
        socklen_t len = sizeof(addr); require(getsockname(fd, (void *)&addr, &len) == 0, "getsockname");
        printf("{\"port\":%u}\n", ntohs(addr.sin_port)); fflush(stdout);
        for (;;) {
            int peer = accept(fd, NULL, NULL);
            if (peer < 0 && errno == EINTR) continue;
            require(peer >= 0, "accept"); serve(peer); require(close(peer) == 0, "close peer");
        }
    }
    require(argc == 6 && !strcmp(argv[1], "client"), "client arguments");
    int rr = !strcmp(argv[4], "rr"); require(rr || !strcmp(argv[4], "stream"), "client mode");
    double duration = atof(argv[5]); require(duration > 0, "duration");
    socket_options(fd); require(connect(fd, (void *)&addr, sizeof(addr)) == 0, "connect");
    uint32_t header[2] = {MAGIC, (uint32_t)rr}; write_all(fd, header, sizeof(header));
    uint64_t count = 0, remote = 0; double start = now();
    do {
        if (rr) {
            unsigned char reply[64]; write_all(fd, pattern, 64); read_all(fd, reply, 64);
            require(memcmp(reply, pattern, 64) == 0, "rr reply"); count++;
        } else { write_all(fd, pattern, sizeof(pattern)); count += sizeof(pattern); }
    } while (now() - start < duration);
    require(shutdown(fd, SHUT_WR) == 0, "shutdown write");
    read_all(fd, &remote, sizeof(remote)); double elapsed = now() - start;
    require(remote == count && count > 0, "server transfer count"); close(fd);
    result(count / elapsed / (rr ? 1.0 : 1e6), count); return 0;
}
