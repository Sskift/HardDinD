/* The exact same static echo server runs outside and inside the guest. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <unistd.h>

static void die(const char *s) { perror(s); exit(1); }
int main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "addresses")) {
        struct ifaddrs *addresses;
        if (getifaddrs(&addresses)) die("getifaddrs");
        int found = 0;
        for (struct ifaddrs *a = addresses; a; a = a->ifa_next) {
            if (!a->ifa_addr || strcmp(a->ifa_name, argv[2])) continue;
            int family = a->ifa_addr->sa_family;
            const void *address;
            if (family == AF_INET)
                address = &((struct sockaddr_in *)a->ifa_addr)->sin_addr;
            else if (family == AF_INET6)
                address = &((struct sockaddr_in6 *)a->ifa_addr)->sin6_addr;
            else continue;
            char text[INET6_ADDRSTRLEN];
            if (!inet_ntop(family, address, text, sizeof(text))) die("inet_ntop");
            printf("%d %s\n", family == AF_INET ? 4 : 6, text);
            found = 1;
        }
        freeifaddrs(addresses);
        return found ? 0 : 1;
    }
    if (argc == 3 && !strcmp(argv[1], "mtu")) {
        struct ifreq request = {0};
        if (strlen(argv[2]) >= sizeof(request.ifr_name)) return 2;
        strcpy(request.ifr_name, argv[2]);
        int fd = socket(AF_INET, SOCK_DGRAM, 0);
        if (fd < 0 || ioctl(fd, SIOCGIFMTU, &request)) die("SIOCGIFMTU");
        printf("%d\n", request.ifr_mtu); close(fd); return 0;
    }
    if ((argc != 3 && argc != 4) || (strcmp(argv[1], "tcp") && strcmp(argv[1], "udp"))) return 2;
    char *end;
    long port = strtol(argv[2], &end, 10);
    if (!*argv[2] || *end || port < 1 || port > 65535) return 2;
    int udp = !strcmp(argv[1], "udp");
    int fd = socket(AF_INET, udp ? SOCK_DGRAM : SOCK_STREAM, 0), one = 1;
    if (fd < 0) die("socket");
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one))) die("setsockopt");
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((unsigned short)port)};
    if (inet_pton(AF_INET, argc == 4 ? argv[3] : "127.0.0.1", &addr.sin_addr) != 1) return 2;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr))) die("bind");
    signal(SIGPIPE, SIG_IGN);
    if (!udp && listen(fd, 8)) die("listen");
    puts("NETWORK_PROBE_READY"); fflush(stdout);
    char data[65536];
    for (;;) {
        if (udp) {
            struct sockaddr_in peer; socklen_t size = sizeof(peer);
            ssize_t n = recvfrom(fd, data, sizeof(data), 0, (struct sockaddr *)&peer, &size);
            if (n < 0) { if (errno == EINTR) continue; die("recvfrom"); }
            if (sendto(fd, data, n, 0, (struct sockaddr *)&peer, size) != n) die("sendto");
        } else {
            int client = accept(fd, NULL, NULL);
            if (client < 0) { if (errno == EINTR) continue; die("accept"); }
            for (;;) {
                ssize_t n = read(client, data, sizeof(data));
                if (!n) break;
                if (n < 0) { if (errno == EINTR) continue; break; }
                ssize_t offset = 0;
                while (offset < n) {
                    ssize_t sent = send(client, data + offset, n - offset, MSG_NOSIGNAL);
                    if (sent < 0 && errno == EINTR) continue;
                    if (sent <= 0) break;
                    offset += sent;
                }
                if (offset != n) break;
            }
            close(client);
        }
    }
}
