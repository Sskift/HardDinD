/* Ethernet over an inherited Unix seqpacket socket, using ordinary host TCP/UDP.
 * No host namespace, tap, firewall or capability changes are needed.
 * Control is a private Unix datagram socket: add/del/list/stats.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <slirp/libslirp.h>

#define MAX_FRAME 65536
#define MAX_FDS 16384
#define MAX_FORWARDS 256
#define MAX_QUEUE 256
#define MAX_BATCH 64
static int link_fd, ctl_fd, nfds;
static unsigned batch_size = 1;
static int mtu = 1500;
static struct pollfd pfds[MAX_FDS];
static volatile sig_atomic_t stopping;
static uint64_t rx_packets, tx_packets, rx_bytes, tx_bytes, dropped_packets;
static uint64_t rx_syscalls, tx_syscalls, poll_calls;
static bool trace_dns;
static struct in_addr guest_addr;
struct timer { bool used; int64_t expires; SlirpTimerCb cb; void *opaque; };
static struct timer timers[64];
struct packet { struct packet *next; size_t len; unsigned char data[]; };
static struct packet *head, *tail;
static unsigned queued;
struct forward { int udp, host_port, guest_port; struct in_addr addr; };
static struct forward forwards[MAX_FORWARDS];
static unsigned nforwards;

static void die(const char *what) { perror(what); exit(1); }
static void stop(int sig) { (void)sig; stopping = 1; }
static int64_t clock_ns(void *opaque) {
    (void)opaque;
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) die("clock_gettime");
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}
static void dns_trace(const void *buf, size_t len, const char *direction) {
    if (!trace_dns || len < 42) return;
    const unsigned char *p = buf;
    if (p[12] != 8 || p[13] || (p[14] >> 4) != 4 || p[23] != 17) return;
    size_t udp = 14 + (p[14] & 15) * 4;
    if (udp + 8 > len || !((p[udp] == 0 && p[udp+1] == 53) || (p[udp+2] == 0 && p[udp+3] == 53))) return;
    printf("HARDDIND_DNS %" PRId64 " %s ", clock_ns(NULL), direction);
    for (size_t i = 0; i < len; i++) printf("%02x", p[i]);
    putchar('\n'); fflush(stdout);
}
static ssize_t send_frame(const void *buf, size_t len, void *opaque) {
    (void)opaque;
    dns_trace(buf, len, "to_guest");
    if (!head && batch_size == 1) {
        tx_syscalls++;
        ssize_t n = send(link_fd, buf, len, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n == (ssize_t)len) { tx_packets++; tx_bytes += len; return n; }
        if (n >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            dropped_packets++; return -1;
        }
    }
    if (queued == MAX_QUEUE || len > MAX_FRAME) { dropped_packets++; return len; }
    struct packet *p = malloc(sizeof(*p) + len);
    if (!p) die("packet allocation");
    p->next = NULL; p->len = len; memcpy(p->data, buf, len);
    if (tail) tail->next = p; else head = p;
    tail = p; queued++;
    return len;
}
static void flush_frames(void) {
    while (head) {
        if (batch_size > 1) {
            struct mmsghdr messages[MAX_BATCH] = {0};
            struct iovec vectors[MAX_BATCH];
            unsigned count = 0;
            for (struct packet *p = head; p && count < batch_size; p = p->next) {
                vectors[count] = (struct iovec){p->data, p->len};
                messages[count].msg_hdr.msg_iov = &vectors[count];
                messages[count].msg_hdr.msg_iovlen = 1;
                count++;
            }
            tx_syscalls++;
            int sent = sendmmsg(link_fd, messages, count, MSG_DONTWAIT | MSG_NOSIGNAL);
            if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) break;
            if (sent == 0) break;
            /* A fatal error consumes only the first failed frame. Successfully
             * sent datagrams are removed exactly once, including partial batches. */
            unsigned consumed = sent < 0 ? 1 : (unsigned)sent;
            for (unsigned i = 0; i < consumed; i++) {
                struct packet *p = head;
                if (sent > 0 && messages[i].msg_len == p->len) { tx_packets++; tx_bytes += p->len; }
                else dropped_packets++;
                head = p->next; free(p); queued--;
            }
            continue;
        }
        tx_syscalls++;
        ssize_t n = send(link_fd, head->data, head->len, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) break;
        if (n == (ssize_t)head->len) { tx_packets++; tx_bytes += head->len; }
        else dropped_packets++;
        struct packet *p = head; head = p->next; free(p); queued--;
    }
    if (!head) tail = NULL;
}
static void guest_error(const char *msg, void *opaque) { (void)opaque; fprintf(stderr, "slirp: %s\n", msg); }
static void *timer_new(SlirpTimerCb cb, void *cb_opaque, void *opaque) {
    (void)opaque;
    for (unsigned i = 0; i < sizeof(timers) / sizeof(*timers); i++) {
        if (!timers[i].used) { timers[i] = (struct timer){true, -1, cb, cb_opaque}; return &timers[i]; }
    }
    fprintf(stderr, "too many slirp timers\n"); exit(1);
}
static void timer_free(void *timer, void *opaque) { (void)opaque; ((struct timer *)timer)->used = false; }
static void timer_mod(void *timer, int64_t expires, void *opaque) { (void)opaque; ((struct timer *)timer)->expires = expires; }
static void poll_register(int fd, void *opaque) { (void)fd; (void)opaque; }
static void notify(void *opaque) { (void)opaque; }
static const SlirpCb callbacks = {
    .send_packet = send_frame, .guest_error = guest_error, .clock_get_ns = clock_ns,
    .timer_new = timer_new, .timer_free = timer_free, .timer_mod = timer_mod,
    .register_poll_fd = poll_register, .unregister_poll_fd = poll_register, .notify = notify
};
static int add_poll(int fd, int events, void *opaque) {
    (void)opaque;
    if (nfds == MAX_FDS) { fprintf(stderr, "poll descriptor limit\n"); exit(1); }
    short ev = 0;
    if (events & SLIRP_POLL_IN) ev |= POLLIN;
    if (events & SLIRP_POLL_OUT) ev |= POLLOUT;
    if (events & SLIRP_POLL_PRI) ev |= POLLPRI;
    pfds[nfds] = (struct pollfd){fd, ev, 0};
    return nfds++;
}
static int get_events(int index, void *opaque) {
    (void)opaque;
    if (index < 0 || index >= nfds) abort();
    short re = pfds[index].revents;
    return ((re & POLLIN) ? SLIRP_POLL_IN : 0) | ((re & POLLOUT) ? SLIRP_POLL_OUT : 0) |
           ((re & POLLPRI) ? SLIRP_POLL_PRI : 0) | ((re & POLLERR) ? SLIRP_POLL_ERR : 0) |
           ((re & POLLHUP) ? SLIRP_POLL_HUP : 0);
}
static uint32_t timers_run(void) {
    int64_t now = clock_ns(NULL) / 1000000;
    uint32_t wait = 1000;
    for (unsigned i = 0; i < sizeof(timers) / sizeof(*timers); i++) {
        struct timer *t = &timers[i];
        if (!t->used || t->expires < 0) continue;
        if (t->expires <= now) { t->expires = -1; t->cb(t->opaque); }
        if (t->used && t->expires >= 0) {
            int64_t delta = t->expires > now ? t->expires - now : 0;
            if (delta < wait) wait = (uint32_t)delta;
        }
    }
    return wait;
}
static int number(const char *s, int minimum, int maximum) {
    char *end;
    errno = 0;
    long n = strtol(s, &end, 10);
    return errno || !*s || *end || n < minimum || n > maximum ? -1 : (int)n;
}
static int stats(char *buf, size_t len) {
    return snprintf(buf, len, "{\"rx_packets\":%" PRIu64 ",\"tx_packets\":%" PRIu64
        ",\"rx_bytes\":%" PRIu64 ",\"tx_bytes\":%" PRIu64
        ",\"dropped_packets\":%" PRIu64 ",\"queued_packets\":%u,\"forwards\":%u"
        ",\"mtu\":%d,\"batch_size\":%u,\"rx_syscalls\":%" PRIu64 ",\"tx_syscalls\":%" PRIu64
        ",\"poll_calls\":%" PRIu64 "}",
        rx_packets, tx_packets, rx_bytes, tx_bytes, dropped_packets, queued, nforwards,
        mtu, batch_size, rx_syscalls, tx_syscalls, poll_calls);
}
static void control(Slirp *slirp) {
    char input[512], output[32768], *fields[7], *save;
    struct sockaddr_un peer;
    socklen_t size = sizeof(peer);
    ssize_t n = recvfrom(ctl_fd, input, sizeof(input) - 1, MSG_DONTWAIT | MSG_TRUNC,
                         (struct sockaddr *)&peer, &size);
    if (n < 0) return;
    const char *error = "invalid command";
    unsigned count = 0;
    if ((size_t)n < sizeof(input) - 1) {
        input[n] = 0;
        for (char *s = strtok_r(input, " \n", &save); s && count < 7; s = strtok_r(NULL, " \n", &save)) fields[count++] = s;
        if (count == 1 && !strcmp(fields[0], "stats")) {
            stats(output, sizeof(output)); goto reply;
        }
        if (count == 1 && !strcmp(fields[0], "list")) {
            size_t used = 0;
            output[used++] = '[';
            for (unsigned i = 0; i < nforwards; i++) {
                struct forward *f = &forwards[i];
                char address[INET_ADDRSTRLEN];
                inet_ntop(AF_INET, &f->addr, address, sizeof(address));
                used += (size_t)snprintf(output + used, sizeof(output) - used,
                    "%s{\"protocol\":\"%s\",\"address\":\"%s\",\"host_port\":%d,\"guest_port\":%d}",
                    i ? "," : "", f->udp ? "udp" : "tcp", address, f->host_port, f->guest_port);
            }
            snprintf(output + used, sizeof(output) - used, "]"); goto reply;
        }
        if (count == 5 && (!strcmp(fields[0], "add") || !strcmp(fields[0], "create") || !strcmp(fields[0], "del"))) {
            int udp = !strcmp(fields[1], "udp");
            struct in_addr addr;
            int hp = number(fields[3], 1, 65535), gp = number(fields[4], 1, 65535);
            if ((udp || !strcmp(fields[1], "tcp")) && inet_pton(AF_INET, fields[2], &addr) == 1 && hp > 0 && gp > 0) {
                unsigned i;
                for (i = 0; i < nforwards; i++)
                    if (forwards[i].udp == udp && forwards[i].addr.s_addr == addr.s_addr && forwards[i].host_port == hp) break;
                if (!strcmp(fields[0], "del")) {
                    if (i == nforwards) error = NULL;
                    else if (forwards[i].guest_port != gp) error = "different guest port";
                    else if (!slirp_remove_hostfwd(slirp, udp, addr, hp)) { forwards[i] = forwards[--nforwards]; error = NULL; }
                    else error = "forward removal failed";
                } else if (i < nforwards) error = !strcmp(fields[0], "create") ? "host port already allocated" : (forwards[i].guest_port == gp ? NULL : "different guest port");
                else if (nforwards == MAX_FORWARDS) error = "forward limit";
                else if (slirp_add_hostfwd(slirp, udp, addr, hp, guest_addr, gp)) error = "host port unavailable";
                else { forwards[nforwards++] = (struct forward){udp, hp, gp, addr}; error = NULL; }
            }
        }
    }
    if (error) snprintf(output, sizeof(output), "{\"ok\":false,\"error\":\"%s\"}", error);
    else strcpy(output, "{\"ok\":true}");
reply:
    if (sendto(ctl_fd, output, strlen(output), MSG_DONTWAIT | MSG_NOSIGNAL, (struct sockaddr *)&peer, size) < 0)
        fprintf(stderr, "control reply: %s\n", strerror(errno));
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(slirp_version_string()); return 0; }
    if ((argc < 3 || argc > 5) || (link_fd = number(argv[1], 0, INT_MAX)) < 0 || strlen(argv[2]) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
        fprintf(stderr, "usage: network-backend LINK_FD CONTROL_SOCKET [BATCH_SIZE [MTU]]\n"); return 2;
    }
    if (strcmp(slirp_version_string(), "4.6.1")) { fprintf(stderr, "expected libslirp 4.6.1\n"); return 1; }
    if (argc >= 4) {
        int value = number(argv[3], 1, MAX_BATCH);
        if (value < 0) { fprintf(stderr, "invalid batch size\n"); return 2; }
        batch_size = (unsigned)value;
    }
    if (argc == 5 && (mtu = number(argv[4], 576, 65520)) < 0) {
        fprintf(stderr, "invalid MTU\n"); return 2;
    }
    trace_dns = getenv("HARDDIND_NET_TRACE_DNS") != NULL;
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGPIPE, SIG_IGN);
    pid_t parent = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGTERM) || getppid() != parent) return 1;
    int flags = fcntl(link_fd, F_GETFL);
    if (flags < 0 || fcntl(link_fd, F_SETFL, flags | O_NONBLOCK)) die("link flags");
    ctl_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (ctl_fd < 0) die("control socket");
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    strcpy(addr.sun_path, argv[2]);
    umask(0077);
    /* Refuse an existing path; never unlink another instance's socket. */
    if (bind(ctl_fd, (struct sockaddr *)&addr, sizeof(addr))) die("control bind");
    SlirpConfig cfg = {.version = 3, .in_enabled = true, .if_mtu = (size_t)mtu, .if_mru = (size_t)mtu};
    inet_pton(AF_INET, "10.0.2.0", &cfg.vnetwork);
    inet_pton(AF_INET, "255.255.255.0", &cfg.vnetmask);
    inet_pton(AF_INET, "10.0.2.2", &cfg.vhost);
    inet_pton(AF_INET, "10.0.2.15", &guest_addr); cfg.vdhcp_start = guest_addr;
    inet_pton(AF_INET, "10.0.2.3", &cfg.vnameserver);
    Slirp *slirp = slirp_new(&cfg, &callbacks, NULL);
    if (!slirp) { unlink(argv[2]); return 1; }
    printf("HARDDIND_NETWORK_READY libslirp=%s\n", slirp_version_string()); fflush(stdout);
    static unsigned char frames[MAX_BATCH][MAX_FRAME];
    while (!stopping) {
        uint32_t timeout = timers_run();
        nfds = 0;
        add_poll(link_fd, SLIRP_POLL_IN | (head ? SLIRP_POLL_OUT : 0), NULL);
        add_poll(ctl_fd, SLIRP_POLL_IN, NULL);
        slirp_pollfds_fill(slirp, &timeout, add_poll, NULL);
        poll_calls++;
        int n = poll(pfds, nfds, (int)timeout);
        if (n < 0) { if (errno == EINTR) continue; die("poll"); }
        if (pfds[0].revents & POLLOUT) flush_frames();
        if (pfds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            unsigned received = 0;
            if (batch_size == 1) {
                /* Keep the reference path as the original one-frame receive
                 * loop, without constructing batch descriptor arrays. */
                for (; received < MAX_BATCH; received++) {
                    rx_syscalls++;
                    ssize_t len = recv(link_fd, frames[0], MAX_FRAME, MSG_DONTWAIT | MSG_TRUNC);
                    if (!len) { stopping = 1; break; }
                    if (len < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) stopping = 1; break; }
                    if (len > MAX_FRAME || len < 14) { dropped_packets++; continue; }
                    rx_packets++; rx_bytes += len;
                    dns_trace(frames[0], (size_t)len, "from_guest");
                    slirp_input(slirp, frames[0], (int)len);
                }
            }
            while (batch_size > 1 && received < MAX_BATCH) {
                struct mmsghdr messages[MAX_BATCH] = {0};
                struct iovec vectors[MAX_BATCH];
                unsigned count = batch_size < MAX_BATCH - received ? batch_size : MAX_BATCH - received;
                for (unsigned i = 0; i < count; i++) {
                    vectors[i] = (struct iovec){frames[i], MAX_FRAME};
                    messages[i].msg_hdr.msg_iov = &vectors[i];
                    messages[i].msg_hdr.msg_iovlen = 1;
                }
                rx_syscalls++;
                int got = recvmmsg(link_fd, messages, count, MSG_DONTWAIT | MSG_TRUNC, NULL);
                if (got < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) stopping = 1; break; }
                if (!got) break;
                for (int i = 0; i < got; i++) {
                    unsigned len = messages[i].msg_len;
                    if (!len) { stopping = 1; break; }
                    if (len > MAX_FRAME || len < 14) { dropped_packets++; continue; }
                    rx_packets++; rx_bytes += len;
                    dns_trace(frames[i], len, "from_guest");
                    slirp_input(slirp, frames[i], (int)len);
                    /* Bound memory even when one receive batch creates many replies. */
                    if (queued >= MAX_BATCH) flush_frames();
                }
                received += (unsigned)got;
                if (stopping) break;
            }
        }
        if (pfds[1].revents & POLLIN) control(slirp);
        slirp_pollfds_poll(slirp, 0, get_events, NULL);
        if (batch_size > 1 && head) flush_frames();
    }
    slirp_cleanup(slirp);
    char report[512]; stats(report, sizeof(report));
    printf("HARDDIND_NETWORK_STATS %s\n", report);
    close(link_fd); close(ctl_fd); unlink(argv[2]);
    while (head) { struct packet *p = head; head = p->next; free(p); }
    return 0;
}
