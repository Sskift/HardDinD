/* Persistent, marker-controlled client for the pinned bench/network protocol. */
#define _GNU_SOURCE
#include "../../bench/common.h"
#include <arpa/inet.h>
#include <linux/ethtool.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <netinet/tcp.h>
#include <sys/ioctl.h>
#include <sys/socket.h>

#define BLOCK 65536
static unsigned char pattern[BLOCK];

static void query(int fd, struct ifreq *ifr, void *data) {
    ifr->ifr_data = data;
    require(ioctl(fd, SIOCETHTOOL, ifr) == 0, "ethtool ioctl");
}

static void inspect(void) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct ifreq ifr = {0};
    strcpy(ifr.ifr_name, "vec0");
    struct { struct ethtool_sset_info info; uint32_t count; } sets = {
        .info = {.cmd = ETHTOOL_GSSET_INFO, .sset_mask = 1ULL << ETH_SS_FEATURES}};
    query(fd, &ifr, &sets);
    require(sets.info.sset_mask && sets.count && sets.count < 1024, "feature count");
    struct ethtool_gstrings *names = calloc(1, sizeof(*names) + sets.count * ETH_GSTRING_LEN);
    require(names != NULL, "feature names");
    names->cmd = ETHTOOL_GSTRINGS; names->string_set = ETH_SS_FEATURES; names->len = sets.count;
    query(fd, &ifr, names);
    unsigned blocks = (sets.count + 31) / 32;
    struct ethtool_gfeatures *features = calloc(1, sizeof(*features) + blocks * sizeof(features->features[0]));
    require(features != NULL, "feature values");
    features->cmd = ETHTOOL_GFEATURES; features->size = blocks;
    query(fd, &ifr, features);
    printf("{\"features\":{");
    for (unsigned i = 0; i < sets.count; i++)
        printf("%s\"%.*s\":%s", i ? "," : "", ETH_GSTRING_LEN, names->data + i * ETH_GSTRING_LEN,
               features->features[i / 32].active & (1U << (i % 32)) ? "true" : "false");
    free(names); free(features);
    struct ethtool_drvinfo info = {.cmd = ETHTOOL_GDRVINFO};
    query(fd, &ifr, &info);
    require(info.n_stats < 1024, "stat count");
    names = calloc(1, sizeof(*names) + info.n_stats * ETH_GSTRING_LEN);
    struct ethtool_stats *stats = calloc(1, sizeof(*stats) + info.n_stats * sizeof(uint64_t));
    require(names && stats, "stats allocation");
    names->cmd = ETHTOOL_GSTRINGS; names->string_set = ETH_SS_STATS; names->len = info.n_stats;
    stats->cmd = ETHTOOL_GSTATS; stats->n_stats = info.n_stats;
    query(fd, &ifr, names); query(fd, &ifr, stats);
    printf("},\"stats\":{");
    for (unsigned i = 0; i < info.n_stats; i++)
        printf("%s\"%.*s\":%llu", i ? "," : "", ETH_GSTRING_LEN, names->data + i * ETH_GSTRING_LEN,
               (unsigned long long)stats->data[i]);
    struct ethtool_ringparam ring = {.cmd = ETHTOOL_GRINGPARAM};
    query(fd, &ifr, &ring);
    printf("},\"rx_depth\":%u,\"tx_depth\":%u}\n", ring.rx_pending, ring.tx_pending);
    free(names); free(stats); close(fd);
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "inspect")) { inspect(); return 0; }
    require(argc == 4, "usage: net-transfer IP PORT stream|rr");
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)atoi(argv[2]))};
    require(inet_pton(AF_INET, argv[1], &addr.sin_addr) == 1, "address");
    int rr = !strcmp(argv[3], "rr");
    require(rr || !strcmp(argv[3], "stream"), "mode");
    for (size_t i = 0; i < BLOCK; i++) pattern[i] = (unsigned char)((i * 131 + i / 251) % 256);
    char line[80];
    while (fgets(line, sizeof(line), stdin)) {
        uint64_t count = strtoull(line, NULL, 10), done = 0, remote;
        require(count > 0, "transfer count");
        int fd = socket(AF_INET, SOCK_STREAM, 0), one = 1;
        struct timeval limit = {.tv_sec = 30};
        require(fd >= 0, "socket");
        require(setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)) == 0, "nodelay");
        require(setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit)) == 0, "receive timeout");
        require(setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit)) == 0, "send timeout");
        require(connect(fd, (void *)&addr, sizeof(addr)) == 0, "connect");
        uint32_t header[2] = {0x48444231U, (uint32_t)rr};
        write_all(fd, header, sizeof(header));
        puts("READY"); fflush(stdout);
        require(fgets(line, sizeof(line), stdin) && !strcmp(line, "GO\n"), "start marker");
        double start = now();
        while (done < count) {
            if (rr) {
                unsigned char reply[64];
                write_all(fd, pattern, 64); read_all(fd, reply, sizeof(reply));
                require(!memcmp(reply, pattern, sizeof(reply)), "reply bytes"); done++;
            } else {
                size_t chunk = count - done < BLOCK ? (size_t)(count - done) : BLOCK;
                write_all(fd, pattern, chunk); done += chunk;
            }
        }
        require(shutdown(fd, SHUT_WR) == 0, "shutdown"); read_all(fd, &remote, sizeof(remote));
        require(remote == count, "server byte count");
        double elapsed = now() - start;
        require(close(fd) == 0, "close");
        printf("{\"verified\":%llu,\"seconds\":%.9g,\"value\":%.9g}\n",
               (unsigned long long)done, elapsed, done / elapsed / (rr ? 1 : 1e6));
        fflush(stdout);
    }
    return 0;
}
