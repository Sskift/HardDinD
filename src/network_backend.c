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
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <libslirp.h>

#define MAX_FRAME 65536
#define MAX_FDS 16384
#define MAX_FORWARDS 4096
#define MAX_QUEUE 256
#define MAX_BATCH 64
static int link_fd, ctl_fd, nfds;
static unsigned batch_size = 1;
static int mtu = 1500;
static struct pollfd pfds[MAX_FDS];
static volatile sig_atomic_t stopping;
static uint64_t rx_packets, tx_packets, rx_bytes, tx_bytes, dropped_packets;
static uint64_t rx_syscalls, tx_syscalls, poll_calls;
static bool trace_dns, trace_tcp;
static bool vnet;
static uint64_t rx_gso, tx_gso, coalesced;
static uint64_t slirp_gso, slirp_gso_bytes;
static uint64_t coalesced_acks;
static uint64_t rx_csum_packets, rx_csum_bytes;
static bool profiling;
static uint64_t input_ns, slirp_poll_ns, rx_ns, tx_ns, wait_ns;
struct io_profile { uint64_t calls, bytes, ns, small, large, blocked, partial, max; };
static struct io_profile host_send, host_recv;
static struct in_addr guest_addr;
struct timer { bool used; int64_t expires; SlirpTimerCb cb; void *opaque; };
static struct timer timers[64];
struct packet { struct packet *next; size_t len, capacity; unsigned gso_size; unsigned char data[]; };
static struct packet *head, *tail;
static unsigned queued;
/* One host forwarding. family is AF_INET or AF_INET6; the address union key
 * matches the concrete address libslirp binds on the host (haddr). The guest
 * alias is the per-family address dockerd reserves/DNATs to inside the guest. */
struct forward {
    int family, udp, host_port, guest_port;
    struct in_addr addr, guest_addr;
    struct in6_addr addr6, guest_addr6;
};
static struct forward forwards[MAX_FORWARDS];
static unsigned nforwards;
/* Bumped on every successful add/remove; paged list clients use it to detect
 * concurrent table changes while aggregating pages. The control loop runs on
 * one thread, so plain 32-bit counters are sufficient. */
static uint32_t inventory_revision;

/* Legacy virtio-net header, little endian, followed by one Ethernet frame. */
#define VNET_SIZE 10
static unsigned be16(const unsigned char *p) { return ((unsigned)p[0] << 8) | p[1]; }
static unsigned le16(const unsigned char *p) { return ((unsigned)p[1] << 8) | p[0]; }
static uint32_t be32(const unsigned char *p) { return (uint32_t)be16(p) << 16 | be16(p + 2); }
static void put16(unsigned char *p, unsigned n) { p[0] = n >> 8; p[1] = n; }
static void putle16(unsigned char *p, unsigned n) { p[0] = n; p[1] = n >> 8; }
static unsigned sum16(const unsigned char *p, size_t len, unsigned sum) {
    for (; len >= 2; len -= 2, p += 2) sum += be16(p);
    if (len) sum += (unsigned)*p << 8;
    return sum;
}
static unsigned finish_sum(unsigned sum) {
    while (sum >> 16) sum = (sum & 65535) + (sum >> 16);
    return (~sum) & 65535;
}
struct tcp_frame { unsigned tcp, payload, size, version; };
static bool tcp_frame(const unsigned char *p, size_t len, struct tcp_frame *f) {
    if (len < 54) return false;
    if (be16(p + 12) == 0x800 && p[14] >> 4 == 4 && (p[14] & 15) >= 5 && p[23] == 6 &&
        !(be16(p + 20) & 0x3fff) && be16(p + 16) + 14u == len) {
        f->tcp = 14 + (p[14] & 15) * 4; f->version = 4;
    } else if (len >= 74 && be16(p + 12) == 0x86dd && p[14] >> 4 == 6 &&
               be16(p + 18) + 54u == len) {
        f->tcp = 54; f->version = 6;
        unsigned next = p[20], count = 0;
        while (next != 6) {
            if ((next != 0 && next != 43 && next != 60) ||
                ++count > 8 || f->tcp + 2 > len) return false;
            unsigned extlen = (p[f->tcp + 1] + 1u) * 8;
            next = p[f->tcp]; f->tcp += extlen;
        }
    } else return false;
    if (f->tcp + 20 > len) return false;
    unsigned hlen = (p[f->tcp + 12] >> 4) * 4;
    if (hlen < 20 || f->tcp + hlen > len) return false;
    f->payload = f->tcp + hlen; f->size = len - f->payload;
    return true;
}
static bool append_tcp(struct packet *q, const unsigned char *p, size_t len) {
    struct tcp_frame a, b;
    if (!q || !q->gso_size || !tcp_frame(q->data, q->len, &a) || !tcp_frame(p, len, &b) ||
        a.tcp != (a.version == 4 ? 34u : 54u) || b.tcp != a.tcp ||
        a.payload != b.payload || a.version != b.version || !b.size || b.size > q->gso_size ||
        a.size % q->gso_size || q->len + b.size > q->capacity ||
        q->data[a.tcp + 13] != 0x10 || (p[b.tcp + 13] & ~8u) != 0x10 ||
        be32(q->data + a.tcp + 4) + a.size != be32(p + b.tcp + 4)) return false;
    /* Require identical flow, ACK, window, options and IP metadata. */
    for (unsigned i = 0; i < a.payload; i++) {
        if (a.version == 4 && ((i >= 16 && i < 20) || (i >= 24 && i < 26))) continue;
        if (a.version == 6 && (i == 18 || i == 19)) continue;
        if ((i >= a.tcp + 4 && i < a.tcp + 8) || i == a.tcp + 13 ||
            i == a.tcp + 16 || i == a.tcp + 17) continue;
        if (q->data[i] != p[i]) return false;
    }
    if (a.version == 4 && (uint16_t)(be16(q->data + 18) + a.size / q->gso_size) != be16(p + 18))
        return false;
    memcpy(q->data + q->len, p + b.payload, b.size);
    q->len += b.size;
    q->data[a.tcp + 13] = p[b.tcp + 13];
    put16(q->data + (a.version == 4 ? 16 : 18), q->len - (a.version == 4 ? 14 : 54));
    coalesced++;
    return true;
}
static bool replace_ack(struct packet *q, const unsigned char *p, size_t len) {
    struct tcp_frame a, b;
    if (!q || q->len != len || !tcp_frame(q->data, q->len, &a) || !tcp_frame(p, len, &b) ||
        a.tcp != (a.version == 4 ? 34u : 54u) || b.tcp != a.tcp || a.version != b.version ||
        a.payload != a.tcp + 20 || b.payload != a.payload || a.size || b.size ||
        q->data[a.tcp + 13] != 0x10 || p[b.tcp + 13] != 0x10 ||
        (int32_t)(be32(p + b.tcp + 8) - be32(q->data + a.tcp + 8)) <= 0) return false;
    /* Only cumulative ACK progress may replace an ACK. Preserve duplicates,
     * sequence changes, ECN, options, and every flow's independent feedback. */
    for (unsigned i = 0; i < a.payload; i++) {
        if (a.version == 4 && ((i >= 18 && i < 20) || (i >= 24 && i < 26))) continue;
        if ((i >= a.tcp + 8 && i < a.tcp + 12) || (i >= a.tcp + 14 && i < a.tcp + 18)) continue;
        if (q->data[i] != p[i]) return false;
    }
    memcpy(q->data, p, len);
    coalesced_acks++;
    return true;
}
static void frame_header(struct packet *p, unsigned char hdr[VNET_SIZE]) {
    memset(hdr, 0, VNET_SIZE);
    struct tcp_frame f;
    if (!p->gso_size || !tcp_frame(p->data, p->len, &f) || f.size <= p->gso_size) return;
    unsigned char *data = p->data;
    unsigned tcp_len = p->len - f.tcp, sum = tcp_len + 6;
    if (f.version == 4) {
        data[24] = data[25] = 0;
        put16(data + 24, finish_sum(sum16(data + 14, 20, 0)));
        sum = sum16(data + 26, 8, sum);
    } else sum = sum16(data + 22, 32, sum);
    put16(data + f.tcp + 16, (~finish_sum(sum)) & 65535);
    hdr[0] = 1; /* NEEDS_CSUM: standard GSO pseudoheader seed. */
    hdr[1] = f.version == 4 ? 1 : 4;
    putle16(hdr + 2, f.payload); putle16(hdr + 4, p->gso_size);
    putle16(hdr + 6, f.tcp); putle16(hdr + 8, 16);
}
static bool input_frame(unsigned char **data, unsigned *len, bool *tcp_checksum_valid) {
    *tcp_checksum_valid = false;
    if (!vnet) return *len >= 14;
    if (*len < VNET_SIZE + 14) return false;
    unsigned char *hdr = *data, *p = hdr + VNET_SIZE;
    *len -= VNET_SIZE; *data = p;
    unsigned type = hdr[1] & ~0x80u;
    if ((hdr[0] & ~3u) || (type != 0 && type != 1 && type != 4)) return false;
    if (type) {
        struct tcp_frame f;
        if (!tcp_frame(p, *len, &f) || !le16(hdr + 4) ||
            type != (f.version == 4 ? 1u : 4u) ||
            le16(hdr + 2) < f.payload || le16(hdr + 2) > *len) return false;
    }
    if (hdr[0] & 1) {
        unsigned start = le16(hdr + 6), offset = le16(hdr + 8);
        if (start > *len || offset + 2 > *len - start) return false;
        struct tcp_frame f;
        if (type && offset == 16 && tcp_frame(p, *len, &f) && start == f.tcp &&
            f.tcp == (f.version == 4 ? 34u : 54u)) {
            unsigned sum = sum16(p + (f.version == 4 ? 26 : 22), f.version == 4 ? 8 : 32,
                                 *len - start + 6);
            *tcp_checksum_valid = be16(p + start + offset) == ((~finish_sum(sum)) & 65535);
        }
        if (*tcp_checksum_valid) {
            rx_csum_packets++; rx_csum_bytes += *len;
        } else {
            unsigned check = finish_sum(sum16(p + start, *len - start, 0));
            put16(p + start + offset, check ? check : 65535);
        }
    }
    if (type) rx_gso++;
    return true;
}

static void die(const char *what) { perror(what); exit(1); }
static void stop(int sig) { (void)sig; stopping = 1; }
static int64_t clock_ns(void *opaque) {
    (void)opaque;
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) die("clock_gettime");
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}
static uint64_t profile_start(void) { return profiling ? (uint64_t)clock_ns(NULL) : 0; }
static void profile_end(uint64_t start, uint64_t *total) {
    if (start) {
        int saved = errno;
        *total += (uint64_t)clock_ns(NULL) - start;
        errno = saved;
    }
}
static void profile_io(struct io_profile *p, uint64_t start, size_t size, ssize_t result) {
    if (!start) return;
    int saved = errno;
    profile_end(start, &p->ns);
    p->calls++;
    if (size < 16384) p->small++; else p->large++;
    if (size > p->max) p->max = size;
    if (result > 0) p->bytes += (uint64_t)result;
    if (result >= 0 && (size_t)result < size) p->partial++;
    if (result < 0 && (saved == EAGAIN || saved == EWOULDBLOCK)) p->blocked++;
    errno = saved;
}
ssize_t __real_send(int fd, const void *buf, size_t len, int flags);
ssize_t __real_recv(int fd, void *buf, size_t len, int flags);
ssize_t __wrap_send(int fd, const void *buf, size_t len, int flags) {
    uint64_t start = profile_start();
    ssize_t n = __real_send(fd, buf, len, flags);
    profile_io(&host_send, start, len, n);
    return n;
}
ssize_t __wrap_recv(int fd, void *buf, size_t len, int flags) {
    uint64_t start = profile_start();
    ssize_t n = __real_recv(fd, buf, len, flags);
    profile_io(&host_recv, start, len, n);
    return n;
}
static void slirp_input_profile(Slirp *slirp, unsigned char *data, int size, bool checksum_valid) {
    uint64_t start = profile_start();
    slirp_input_checksum(slirp, data, size, checksum_valid);
    profile_end(start, &input_ns);
}
static void tcp_trace_frame(const void *buf, size_t len, const char *direction) {
    const unsigned char *p = buf;
    struct tcp_frame f;
    if (!tcp_frame(p, len, &f) || (be16(p + f.tcp) != 443 && be16(p + f.tcp + 2) != 443)) return;
    char src[INET6_ADDRSTRLEN], dst[INET6_ADDRSTRLEN];
    unsigned family = f.version == 4 ? AF_INET : AF_INET6;
    inet_ntop(family, p + (f.version == 4 ? 26 : 22), src, sizeof(src));
    inet_ntop(family, p + (f.version == 4 ? 30 : 38), dst, sizeof(dst));
    unsigned sum = sum16(p + (f.version == 4 ? 26 : 22), f.version == 4 ? 8 : 32, len - f.tcp + 6);
    printf("HARDDIND_TCP %" PRId64 " %s %s:%u %s:%u flags=%02x seq=%" PRIu32
           " ack=%" PRIu32 " win=%u payload=%u checksum=%04x\n", clock_ns(NULL), direction,
           src, be16(p + f.tcp), dst, be16(p + f.tcp + 2), p[f.tcp + 13],
           be32(p + f.tcp + 4), be32(p + f.tcp + 8), be16(p + f.tcp + 14), f.size,
           finish_sum(sum16(p + f.tcp, len - f.tcp, sum)));
    fflush(stdout);
}
static void dns_trace(const void *buf, size_t len, const char *direction) {
    if (trace_tcp) tcp_trace_frame(buf, len, direction);
    if (!trace_dns || len < 42) return;
    const unsigned char *p = buf;
    if (p[12] != 8 || p[13] || (p[14] >> 4) != 4 || p[23] != 17) return;
    size_t udp = 14 + (p[14] & 15) * 4;
    if (udp + 8 > len || !((p[udp] == 0 && p[udp+1] == 53) || (p[udp+2] == 0 && p[udp+3] == 53))) return;
    printf("HARDDIND_DNS %" PRId64 " %s ", clock_ns(NULL), direction);
    for (size_t i = 0; i < len; i++) printf("%02x", p[i]);
    putchar('\n'); fflush(stdout);
}
static ssize_t queue_frame(const void *buf, size_t len, unsigned segment_size) {
    dns_trace(buf, len, segment_size ? "to_guest_gso" : "to_guest");
    if (!head && batch_size == 1 && !vnet) {
        tx_syscalls++;
        ssize_t n = send(link_fd, buf, len, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n == (ssize_t)len) { tx_packets++; tx_bytes += len; return n; }
        if (n >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            dropped_packets++; return -1;
        }
    }
    if (len > MAX_FRAME) { dropped_packets++; return len; }
    if (vnet && !segment_size && (replace_ack(tail, buf, len) || append_tcp(tail, buf, len))) return len;
    if (queued == MAX_QUEUE) { dropped_packets++; return len; }
    struct tcp_frame tcp = {0};
    unsigned gso_size = segment_size ? segment_size :
        (vnet && tcp_frame(buf, len, &tcp) && tcp.size ? tcp.size : 0);
    size_t capacity = gso_size && !segment_size && ((const unsigned char *)buf)[tcp.tcp + 13] == 0x10
        ? MAX_FRAME : len;
    struct packet *p = malloc(sizeof(*p) + capacity);
    if (!p) die("packet allocation");
    p->next = NULL; p->len = len; p->capacity = capacity; p->gso_size = gso_size; memcpy(p->data, buf, len);
    if (tail) tail->next = p; else head = p;
    tail = p; queued++;
    return len;
}
static ssize_t send_frame(const void *buf, size_t len, void *opaque) {
    (void)opaque;
    return queue_frame(buf, len, 0);
}
static ssize_t send_gso(const void *buf, size_t len, unsigned mss, void *opaque) {
    (void)opaque;
    struct tcp_frame tcp;
    if (!vnet || !mss || !tcp_frame(buf, len, &tcp) || tcp.size <= mss ||
        mss + tcp.payload - 14 > (unsigned)mtu) {
        dropped_packets++; return -1;
    }
    slirp_gso++; slirp_gso_bytes += len;
    return queue_frame(buf, len, mss);
}
static void flush_frames(void) {
    uint64_t start = profile_start();
    while (head) {
        if (vnet && head == tail) {
            unsigned char header[VNET_SIZE];
            frame_header(head, header);
            if (trace_tcp) tcp_trace_frame(head->data, head->len,
                header[0] & 1 ? "wire_partial_to_guest" : "wire_to_guest");
            struct iovec vectors[2] = {{header, sizeof(header)}, {head->data, head->len}};
            struct msghdr message = {.msg_iov = vectors, .msg_iovlen = 2};
            tx_syscalls++;
            ssize_t sent = sendmsg(link_fd, &message, MSG_DONTWAIT | MSG_NOSIGNAL);
            if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) break;
            if (sent == (ssize_t)(head->len + VNET_SIZE)) {
                tx_packets++; tx_bytes += head->len;
                if (header[1]) tx_gso++;
            } else dropped_packets++;
            free(head); head = tail = NULL; queued--;
            continue;
        }
        if (batch_size > 1 || vnet) {
            struct mmsghdr messages[MAX_BATCH] = {0};
            struct iovec vectors[MAX_BATCH][2];
            unsigned char headers[MAX_BATCH][VNET_SIZE];
            unsigned count = 0;
            for (struct packet *p = head; p && count < batch_size; p = p->next) {
                frame_header(p, headers[count]);
                if (trace_tcp && vnet) tcp_trace_frame(p->data, p->len,
                    headers[count][0] & 1 ? "wire_partial_to_guest" : "wire_to_guest");
                vectors[count][0] = (struct iovec){headers[count], VNET_SIZE};
                vectors[count][1] = (struct iovec){p->data, p->len};
                messages[count].msg_hdr.msg_iov = &vectors[count][vnet ? 0 : 1];
                messages[count].msg_hdr.msg_iovlen = vnet ? 2 : 1;
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
                if (sent > 0 && messages[i].msg_len == p->len + (vnet ? VNET_SIZE : 0)) {
                    tx_packets++; tx_bytes += p->len;
                    if (vnet && headers[i][1]) tx_gso++;
                }
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
    profile_end(start, &tx_ns);
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
    .send_packet = send_frame, .send_packet_gso = send_gso,
    .guest_error = guest_error, .clock_get_ns = clock_ns,
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
    struct rusage usage = {0};
    int sndbuf, rcvbuf;
    socklen_t size = sizeof(sndbuf);
    if (getsockopt(link_fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, &size) ||
        getsockopt(link_fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, &size)) die("link buffers");
    if (getrusage(RUSAGE_SELF, &usage)) die("getrusage");
    return snprintf(buf, len, "{\"rx_packets\":%" PRIu64 ",\"tx_packets\":%" PRIu64
        ",\"rx_bytes\":%" PRIu64 ",\"tx_bytes\":%" PRIu64
        ",\"dropped_packets\":%" PRIu64 ",\"queued_packets\":%u,\"forwards\":%u"
        ",\"inventory_revision\":%" PRIu32
        ",\"mtu\":%d,\"batch_size\":%u,\"rx_syscalls\":%" PRIu64 ",\"tx_syscalls\":%" PRIu64
        ",\"poll_calls\":%" PRIu64 ",\"vnet\":%s,\"rx_gso\":%" PRIu64
        ",\"tx_gso\":%" PRIu64 ",\"coalesced\":%" PRIu64
        ",\"slirp_gso\":%" PRIu64 ",\"slirp_gso_bytes\":%" PRIu64
        ",\"coalesced_acks\":%" PRIu64
        ",\"rx_csum_packets\":%" PRIu64 ",\"rx_csum_bytes\":%" PRIu64
        ",\"link_sndbuf\":%d,\"link_rcvbuf\":%d"
        ",\"user_us\":%" PRId64 ",\"system_us\":%" PRId64
        ",\"input_ns\":%" PRIu64 ",\"slirp_poll_ns\":%" PRIu64
        ",\"rx_ns\":%" PRIu64 ",\"tx_ns\":%" PRIu64 ",\"wait_ns\":%" PRIu64
        ",\"host_send_calls\":%" PRIu64 ",\"host_send_bytes\":%" PRIu64 ",\"host_send_ns\":%" PRIu64
        ",\"host_send_small\":%" PRIu64 ",\"host_send_large\":%" PRIu64 ",\"host_send_blocked\":%" PRIu64
        ",\"host_send_partial\":%" PRIu64 ",\"host_send_max\":%" PRIu64
        ",\"host_recv_calls\":%" PRIu64 ",\"host_recv_bytes\":%" PRIu64 ",\"host_recv_ns\":%" PRIu64 "}",
        rx_packets, tx_packets, rx_bytes, tx_bytes, dropped_packets, queued, nforwards,
        inventory_revision, mtu, batch_size, rx_syscalls, tx_syscalls, poll_calls,
        vnet ? "true" : "false", rx_gso, tx_gso, coalesced, slirp_gso, slirp_gso_bytes, coalesced_acks,
        rx_csum_packets, rx_csum_bytes,
        sndbuf, rcvbuf,
        (int64_t)usage.ru_utime.tv_sec * 1000000 + usage.ru_utime.tv_usec,
        (int64_t)usage.ru_stime.tv_sec * 1000000 + usage.ru_stime.tv_usec,
        input_ns, slirp_poll_ns, rx_ns, tx_ns, wait_ns,
        host_send.calls, host_send.bytes, host_send.ns, host_send.small, host_send.large,
        host_send.blocked, host_send.partial, host_send.max,
        host_recv.calls, host_recv.bytes, host_recv.ns);
}

/*
 * Bounded JSON inventory transport.
 *
 * Legacy bare "list" returns the original JSON array for small inventories
 * but fails closed with an explicit error instead of truncating when the
 * result would overflow. Paged "list <offset> <limit>" returns a revisioned
 * object so clients can aggregate a complete, consistent snapshot:
 *   {"ok":true,"revision":N,"count":T,"offset":O,"limit":L,"forwards":[...]}
 * The control loop is single threaded, so a revision change between pages
 * means the table changed and the client must restart the sweep.
 */
#define INVENTORY_BUF 32768u
#define INVENTORY_PAGE_MAX 64u
/* Worst-case per-record JSON with INET6_ADDRSTRLEN addresses:
 *   {"protocol":"udp","family":"udp6","address":"<46>","host_port":65535,
 *    "guest_address":"<46>","guest_port":65535},  == 196 bytes. */
#define INVENTORY_RECORD_MAX 196u
/* Machine-checked page envelope bound: 64 max-size records plus the object
 * envelope must always fit the fixed reply buffer. */
_Static_assert(INVENTORY_PAGE_MAX * INVENTORY_RECORD_MAX + 256 < INVENTORY_BUF,
               "paged inventory must fit the fixed control buffer");

struct json_buf {
    char *b;
    size_t cap, used;
    bool overflow;
};

static void jb_init(struct json_buf *j, char *b, size_t cap) {
    j->b = b; j->cap = cap; j->used = 0; j->overflow = false;
}

static void jb_append(struct json_buf *j, const char *fmt, ...) {
    if (j->overflow) return;
    va_list ap;
    va_start(ap, fmt);
    int r = vsnprintf(j->b + j->used, j->cap - j->used, fmt, ap);
    va_end(ap);
    /* Fail closed on truncation (or encoding errors); never advance used past
     * cap (which would turn the next size into an underflowed huge value). */
    if (r < 0 || (size_t)r >= j->cap - j->used) {
        j->overflow = true;
        return;
    }
    j->used += (size_t)r;
}

static void forward_json(struct json_buf *j, const struct forward *f) {
    char address[INET6_ADDRSTRLEN], guest[INET6_ADDRSTRLEN];
    inet_ntop(f->family, f->family == AF_INET ? (void *)&f->addr : (void *)&f->addr6,
              address, sizeof(address));
    inet_ntop(f->family, f->family == AF_INET ? (void *)&f->guest_addr : (void *)&f->guest_addr6,
              guest, sizeof(guest));
    jb_append(j, "{\"protocol\":\"%s\",\"family\":\"%s%d\",\"address\":\"%s\",\"host_port\":%d,"
              "\"guest_address\":\"%s\",\"guest_port\":%d}",
              f->udp ? "udp" : "tcp", f->udp ? "udp" : "tcp",
              f->family == AF_INET ? 4 : 6, address, f->host_port, guest, f->guest_port);
}

/* Legacy array listing; fails closed with an explicit error on overflow. */
static void inventory_legacy(char *output, size_t cap) {
    struct json_buf j; jb_init(&j, output, cap);
    jb_append(&j, "[");
    for (unsigned i = 0; i < nforwards; i++) {
        jb_append(&j, "%s", i ? "," : "");
        forward_json(&j, &forwards[i]);
    }
    jb_append(&j, "]");
    if (j.overflow) {
        snprintf(output, cap, "{\"ok\":false,\"error\":\"inventory too large for legacy list "
                 "buffer; use paged 'list <offset> <limit>'\",\"revision\":%" PRIu32
                 ",\"count\":%u,\"limit\":%u}", inventory_revision, nforwards, INVENTORY_PAGE_MAX);
    }
}

/* Revisioned page listing. Returns false (with an error reply) on bad bounds
 * or the impossible overflow; *revision_change stays false on success. */
static void inventory_page(char *output, size_t cap, unsigned offset, unsigned limit) {
    if (limit == 0 || limit > INVENTORY_PAGE_MAX || offset > nforwards) {
        snprintf(output, cap, "{\"ok\":false,\"error\":\"invalid inventory page\"}");
        return;
    }
    /* Defense in depth: the 64-record bound plus 196-byte record bound makes
     * overflow impossible; still route any arithmetic surprise to fail closed. */
    struct json_buf j; jb_init(&j, output, cap);
    jb_append(&j, "{\"ok\":true,\"revision\":%" PRIu32 ",\"count\":%u,\"offset\":%u,"
              "\"limit\":%u,\"forwards\":[", inventory_revision, nforwards, offset, limit);
    unsigned end = nforwards - offset;
    if (end > limit) end = limit;
    for (unsigned k = 0; k < end; k++) {
        jb_append(&j, "%s", k ? "," : "");
        forward_json(&j, &forwards[offset + k]);
    }
    jb_append(&j, "]}");
    if (j.overflow) {
        snprintf(output, cap, "{\"ok\":false,\"error\":\"inventory page overflow\"}");
    }
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
        if (count == 2 && !strcmp(fields[0], "profile") &&
            (!strcmp(fields[1], "on") || !strcmp(fields[1], "off"))) {
            profiling = !strcmp(fields[1], "on");
            snprintf(output, sizeof(output), "{\"ok\":true}"); goto reply;
        }
        if (count == 2 && !strcmp(fields[0], "tracer")) {
            int pid = number(fields[1], 0, INT_MAX);
            if (pid < 0 || (pid && !profiling)) goto invalid;
            /* Permit only the profiling pipeline and its strace child. This
             * grants no host capability and never changes Yama globally. */
            if (prctl(PR_SET_PTRACER, (unsigned long)pid, 0UL, 0UL, 0UL)) {
                snprintf(output, sizeof(output), "{\"ok\":false,\"error\":\"tracer: %s\"}", strerror(errno));
                goto reply;
            }
            snprintf(output, sizeof(output), "{\"ok\":true}"); goto reply;
        }
        if (count == 1 && !strcmp(fields[0], "list")) {
            inventory_legacy(output, sizeof(output)); goto reply;
        }
        if (count == 3 && !strcmp(fields[0], "list")) {
            int poff = number(fields[1], 0, MAX_FORWARDS);
            int plimit = number(fields[2], 1, INVENTORY_PAGE_MAX);
            if (poff < 0 || plimit < 0) {
                snprintf(output, sizeof(output), "{\"ok\":false,\"error\":\"invalid inventory page\"}");
            } else {
                inventory_page(output, sizeof(output), (unsigned)poff, (unsigned)plimit);
            }
            goto reply;
        }
        if ((count == 5 || count == 6) && (!strcmp(fields[0], "add") || !strcmp(fields[0], "create") || !strcmp(fields[0], "del"))) {
            int udp = !strcmp(fields[1], "udp");
            int family = 0;
            struct in_addr addr4, guest4;
            struct in6_addr addr6, guest6;
            int hp = number(fields[3], 1, 65535), gp = number(fields[4], 1, 65535);
            if (udp || !strcmp(fields[1], "tcp")) {
                if (inet_pton(AF_INET, fields[2], &addr4) == 1) {
                    family = AF_INET;
                    if (count == 6) {
                        if (inet_pton(AF_INET, fields[5], &guest4) != 1) family = 0;
                    } else {
                        guest4 = guest_addr; /* historical 5-field grammar */
                    }
                } else if (inet_pton(AF_INET6, fields[2], &addr6) == 1) {
                    family = AF_INET6;
                    if (count == 6) {
                        if (inet_pton(AF_INET6, fields[5], &guest6) != 1) family = 0;
                    } else {
                        inet_pton(AF_INET6, "fd00::15", &guest6);
                    }
                    /* A scoped literal (fe80::1%dev) is ambiguous over the
                     * text protocol and cannot bind without a zone. */
                    if (strchr(fields[2], '%') || (count == 6 && strchr(fields[5], '%'))) family = 0;
                    /* Reject IPv4-mapped IPv6: it would silently bind IPv4. */
                    if (family == AF_INET6 && IN6_IS_ADDR_V4MAPPED(&addr6)) family = 0;
                }
            }
            if (family && hp > 0 && gp > 0) {
                struct sockaddr_storage haddr = {0}, gaddr = {0};
                socklen_t hlen, glen;
                if (family == AF_INET) {
                    struct sockaddr_in *h = (struct sockaddr_in *)&haddr, *g = (struct sockaddr_in *)&gaddr;
                    h->sin_family = g->sin_family = AF_INET;
                    h->sin_addr = addr4; h->sin_port = htons(hp);
                    g->sin_addr = guest4; g->sin_port = htons(gp);
                    hlen = glen = sizeof(*h);
                } else {
                    struct sockaddr_in6 *h = (struct sockaddr_in6 *)&haddr, *g = (struct sockaddr_in6 *)&gaddr;
                    h->sin6_family = g->sin6_family = AF_INET6;
                    h->sin6_addr = addr6; h->sin6_port = htons(hp);
                    g->sin6_addr = guest6; g->sin6_port = htons(gp);
                    hlen = glen = sizeof(*h);
                }
                int xflags = (family == AF_INET6 ? SLIRP_HOSTFWD_V6ONLY : 0);
                unsigned i;
                for (i = 0; i < nforwards; i++) {
                    if (forwards[i].family != family || forwards[i].udp != udp ||
                        forwards[i].host_port != hp) continue;
                    int same = family == AF_INET ? forwards[i].addr.s_addr == addr4.s_addr
                                                 : memcmp(&forwards[i].addr6, &addr6, sizeof(addr6)) == 0;
                    if (same) break;
                }
                if (!strcmp(fields[0], "del")) {
                    if (i == nforwards) error = NULL;
                    else if (forwards[i].guest_port != gp) error = "different guest port";
                    else if (!slirp_remove_hostxfwd(slirp, (struct sockaddr *)&haddr, hlen,
                                                    udp ? SLIRP_HOSTFWD_UDP : 0)) {
                        forwards[i] = forwards[--nforwards]; inventory_revision++; error = NULL;
                    }
                    else error = "forward removal failed";
                } else if (i < nforwards) {
                    error = !strcmp(fields[0], "create") ? "host port already allocated"
                        : (forwards[i].guest_port == gp ? NULL : "different guest port");
                } else if (nforwards == MAX_FORWARDS) error = "forward limit";
                else if (slirp_add_hostxfwd(slirp, (struct sockaddr *)&haddr, hlen,
                                            (struct sockaddr *)&gaddr, glen,
                                            (udp ? SLIRP_HOSTFWD_UDP : 0) | xflags)) {
                    snprintf(output, sizeof(output),
                        "{\"ok\":false,\"error\":\"host port unavailable: %s\"}", strerror(errno));
                    goto reply_raw;
                } else {
                    struct forward *f = &forwards[nforwards++];
                    f->family = family; f->udp = udp; f->host_port = hp; f->guest_port = gp;
                    if (family == AF_INET) { f->addr = addr4; f->guest_addr = guest4; }
                    else { f->addr6 = addr6; f->guest_addr6 = guest6; }
                    inventory_revision++;
                    error = NULL;
                }
            }
        }
    }
invalid:
    if (error) snprintf(output, sizeof(output), "{\"ok\":false,\"error\":\"%s\"}", error);
    else strcpy(output, "{\"ok\":true}");
reply:
reply_raw:
    if (sendto(ctl_fd, output, strlen(output), MSG_DONTWAIT | MSG_NOSIGNAL, (struct sockaddr *)&peer, size) < 0)
        fprintf(stderr, "control reply: %s\n", strerror(errno));
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(slirp_version_string()); return 0; }
    if ((argc < 3 || argc > 6) || (link_fd = number(argv[1], 0, INT_MAX)) < 0 || strlen(argv[2]) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
        fprintf(stderr, "usage: network-backend LINK_FD CONTROL_SOCKET [BATCH_SIZE [MTU [vnet]]]\n"); return 2;
    }
    if (strcmp(slirp_version_string(), "4.6.1")) { fprintf(stderr, "expected libslirp 4.6.1\n"); return 1; }
    if (argc >= 4) {
        int value = number(argv[3], 1, MAX_BATCH);
        if (value < 0) { fprintf(stderr, "invalid batch size\n"); return 2; }
        batch_size = (unsigned)value;
    }
    if (argc >= 5 && (mtu = number(argv[4], 576, 65520)) < 0) {
        fprintf(stderr, "invalid MTU\n"); return 2;
    }
    if (argc == 6) {
        if (strcmp(argv[5], "vnet")) { fprintf(stderr, "invalid framing\n"); return 2; }
        vnet = true;
    }
    trace_dns = getenv("HARDDIND_NET_TRACE_DNS") != NULL;
    trace_tcp = getenv("HARDDIND_NET_TRACE_TCP") != NULL;
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
    SlirpConfig cfg = {.version = 3, .in_enabled = true, .in6_enabled = true,
                       .if_mtu = (size_t)mtu, .if_mru = vnet ? 65521 : (size_t)mtu};
    inet_pton(AF_INET, "10.0.2.0", &cfg.vnetwork);
    inet_pton(AF_INET, "255.255.255.0", &cfg.vnetmask);
    inet_pton(AF_INET, "10.0.2.2", &cfg.vhost);
    inet_pton(AF_INET, "10.0.2.15", &guest_addr); cfg.vdhcp_start = guest_addr;
    inet_pton(AF_INET, "10.0.2.3", &cfg.vnameserver);
    /* The guest assigns fd00::15 plus the harddind alias block statically;
     * slirp only needs the matching prefix, gateway and resolver. */
    cfg.vprefix_len = 64;
    inet_pton(AF_INET6, "fd00::", &cfg.vprefix_addr6);
    inet_pton(AF_INET6, "fd00::2", &cfg.vhost6);
    inet_pton(AF_INET6, "fd00::3", &cfg.vnameserver6);
    Slirp *slirp = slirp_new(&cfg, &callbacks, NULL);
    if (!slirp) { unlink(argv[2]); return 1; }
    printf("HARDDIND_NETWORK_READY libslirp=%s\n", slirp_version_string()); fflush(stdout);
    static unsigned char frames[MAX_BATCH][MAX_FRAME + VNET_SIZE];
    while (!stopping) {
        uint32_t timeout = timers_run();
        nfds = 0;
        add_poll(link_fd, SLIRP_POLL_IN | (head ? SLIRP_POLL_OUT : 0), NULL);
        add_poll(ctl_fd, SLIRP_POLL_IN, NULL);
        slirp_pollfds_fill(slirp, &timeout, add_poll, NULL);
        poll_calls++;
        uint64_t started = profile_start();
        int n = poll(pfds, nfds, (int)timeout);
        profile_end(started, &wait_ns);
        if (n < 0) { if (errno == EINTR) continue; die("poll"); }
        if (pfds[0].revents & POLLOUT) flush_frames();
        if (pfds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            unsigned received = 0;
            bool bulk = false;
            if (batch_size == 1) {
                /* Keep the reference path as the original one-frame receive
                 * loop, without constructing batch descriptor arrays. */
                for (; received < MAX_BATCH; received++) {
                    rx_syscalls++;
                    started = profile_start();
                    ssize_t len = recv(link_fd, frames[0], sizeof(frames[0]), MSG_DONTWAIT | MSG_TRUNC);
                    profile_end(started, &rx_ns);
                    if (!len) { stopping = 1; break; }
                    if (len < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) stopping = 1; break; }
                    unsigned size = (unsigned)len;
                    unsigned char *data = frames[0];
                    bool checksum_valid;
                    if (size > MAX_FRAME + (vnet ? VNET_SIZE : 0) || !input_frame(&data, &size, &checksum_valid)) {
                        dropped_packets++; continue;
                    }
                    rx_packets++; rx_bytes += size;
                    dns_trace(data, size, checksum_valid ? "from_guest_partial" : "from_guest");
                    slirp_input_profile(slirp, data, (int)size, checksum_valid);
                    if (vnet && queued >= MAX_BATCH) flush_frames();
                }
            }
            while (batch_size > 1 && received < MAX_BATCH) {
                struct mmsghdr messages[MAX_BATCH] = {0};
                struct iovec vectors[MAX_BATCH];
                unsigned count = batch_size < MAX_BATCH - received ? batch_size : MAX_BATCH - received;
                for (unsigned i = 0; i < count; i++) {
                    vectors[i] = (struct iovec){frames[i], sizeof(frames[i])};
                    messages[i].msg_hdr.msg_iov = &vectors[i];
                    messages[i].msg_hdr.msg_iovlen = 1;
                }
                rx_syscalls++;
                started = profile_start();
                int got = recvmmsg(link_fd, messages, count, MSG_DONTWAIT | MSG_TRUNC, NULL);
                profile_end(started, &rx_ns);
                if (got < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) stopping = 1; break; }
                if (!got) break;
                if (got > 1) bulk = true;
                for (int i = 0; i < got; i++) {
                    unsigned len = messages[i].msg_len;
                    if (!len) { stopping = 1; break; }
                    unsigned char *data = frames[i];
                    bool checksum_valid;
                    if (len > MAX_FRAME + (vnet ? VNET_SIZE : 0) || !input_frame(&data, &len, &checksum_valid)) {
                        dropped_packets++; continue;
                    }
                    rx_packets++; rx_bytes += len;
                    dns_trace(data, len, checksum_valid ? "from_guest_partial" : "from_guest");
                    slirp_input_profile(slirp, data, (int)len, checksum_valid);
                    if (len > (unsigned)mtu + 14 || (tail && tail->len > (unsigned)mtu + 14)) bulk = true;
                    /* Bound memory even when one receive batch creates many replies. */
                    if (queued >= MAX_BATCH) flush_frames();
                }
                received += (unsigned)got;
                /* Drain bulk traffic across short batches; only isolated small
                 * frames can avoid the final empty receive without extra polls. */
                if (stopping || (!bulk && (unsigned)got < count)) break;
            }
        }
        if (pfds[1].revents & POLLIN) control(slirp);
        started = profile_start();
        slirp_pollfds_poll(slirp, 0, get_events, NULL);
        profile_end(started, &slirp_poll_ns);
        if ((batch_size > 1 || vnet) && head) flush_frames();
    }
    slirp_cleanup(slirp);
    char report[4096]; stats(report, sizeof(report));
    printf("HARDDIND_NETWORK_STATS %s\n", report);
    close(link_fd); close(ctl_fd); unlink(argv[2]);
    while (head) { struct packet *p = head; head = p->next; free(p); }
    return 0;
}
