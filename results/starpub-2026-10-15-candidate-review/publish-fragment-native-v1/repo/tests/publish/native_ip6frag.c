/* Native unit tests for the HardDinD IPv6 fragmentation code compiled into
 * the real static libslirp. No sockets/tap: mbufs are fed straight into
 * ip6_frag_input() using internal headers, and the virtual monotonic clock is
 * fully controlled so 60 s expiry at/after 2^32 ms uptime needs no real wait.
 *
 * Build (see Makefile): gcc native_ip6frag.c libslirp-harddind.a -lglib-2.0
 */
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa//inet.h>

#include "slirp.h"
#include "ip6_frag.h"

static int failures;
#define CHECK(cond, ...)                                                      \
    do {                                                                      \
        if (!(cond)) {                                                        \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);                       \
            printf(__VA_ARGS__);                                              \
            printf("\n");                                                     \
            failures++;                                                       \
        }                                                                     \
    } while (0)

static uint64_t g_now_ns;
static int64_t test_clock(void *opaque)
{
    (void)opaque;
    return (int64_t)g_now_ns;
}
static void test_register(int fd, void *opaque)
{
    (void)fd;
    (void)opaque;
}
static void test_unregister(int fd, void *opaque)
{
    (void)fd;
    (void)opaque;
}
static void test_notify(void *opaque)
{
    (void)opaque;
}
static ssize_t test_send(const void *buf, size_t len, void *opaque)
{
    (void)buf;
    (void)opaque;
    return (ssize_t)len;
}
/* Reassembly expiry is driven directly via ip6_frag_slowtimo, so framework
 * timers do nothing. */
static void *test_timer_new_stub(SlirpTimerCb cb, void *o, void *x);
static void test_timer_free_stub(void *t, void *o);
static void test_timer_mod_stub(void *t, int64_t e, void *o);

static const uint8_t SRC[16] = {0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x15};
static const uint8_t DST[16] = {0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x02};

static Slirp *make_slirp(void)
{
    static const SlirpCb cb = {
        .send_packet = test_send,
        .clock_get_ns = test_clock,
        .timer_new = test_timer_new_stub,
        .timer_free = test_timer_free_stub,
        .timer_mod = test_timer_mod_stub,
        .register_poll_fd = test_register,
        .unregister_poll_fd = test_unregister,
        .notify = test_notify,
    };
    SlirpConfig cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.version = 3;
    cfg.in_enabled = 1;
    cfg.in6_enabled = 1;
    cfg.if_mtu = 1500;
    cfg.if_mru = 1500;
    inet_pton(AF_INET, "10.0.2.0", &cfg.vnetwork);
    inet_pton(AF_INET, "255.255.255.0", &cfg.vnetmask);
    inet_pton(AF_INET, "10.0.2.2", &cfg.vhost);
    inet_pton(AF_INET, "10.0.2.15", &cfg.vdhcp_start);
    inet_pton(AF_INET, "10.0.2.3", &cfg.vnameserver);
    cfg.vprefix_len = 64;
    inet_pton(AF_INET6, "fd00::", &cfg.vprefix_addr6);
    inet_pton(AF_INET6, "fd00::2", &cfg.vhost6);
    inet_pton(AF_INET6, "fd00::3", &cfg.vnameserver6);
    Slirp *s = slirp_new(&cfg, &cb, NULL);
    CHECK(s != NULL, "slirp_new");
    return s;
}

static void set_v6hdr(struct ip6 *h, uint16_t payload_len, uint8_t nh,
                      uint8_t hop)
{
    memset(h, 0, sizeof(*h));
    uint32_t vtf = htonl(0x60000000u);
    memcpy(h, &vtf, 4);
    h->ip_pl = htons(payload_len);
    h->ip_nh = nh;
    h->ip_hl = hop;
    memcpy(&h->ip_src, SRC, 16);
    memcpy(&h->ip_dst, DST, 16);
}

/* Build one fragment mbuf: fixed header + 8-byte fragment header + body. */
static struct mbuf *make_frag(Slirp *s, uint8_t nxt, uint8_t hop, uint32_t id,
                              unsigned off8, int more, uint8_t res_byte,
                              uint16_t res_bits, const uint8_t *body,
                              unsigned bodylen)
{
    struct mbuf *m = m_get(s);
    m_inc(m, 40 + 8 + (int)bodylen);
    struct ip6 *h = mtod(m, struct ip6 *);
    set_v6hdr(h, (uint16_t)(8 + bodylen), IPPROTO_FRAGMENT, hop);
    uint8_t *p = (uint8_t *)m->m_data + 40;
    p[0] = nxt;
    p[1] = res_byte;
    uint16_t of = htons((uint16_t)((off8 << 3) | (more ? 1 : 0) | res_bits));
    memcpy(p + 2, &of, 2);
    uint32_t nid = htonl(id);
    memcpy(p + 4, &nid, 4);
    memcpy(p + 8, body, bodylen);
    m->m_len = 40 + 8 + (int)bodylen;
    return m;
}

static unsigned count_queues(Slirp *s)
{
    unsigned n = 0;
    for (struct ip6fq *q = s->ip6_reassq; q; q = q->next) {
        n++;
    }
    return n;
}

/* Feed one first non-final fragment to create a live queue. */
static void make_live_queue(Slirp *s, uint32_t id)
{
    uint8_t body[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    struct mbuf *m = make_frag(s, IPPROTO_UDP, 64, id, 0, 1, 0, 0, body, 8);
    struct mbuf *out = ip6_frag_input(s, m);
    CHECK(out == NULL, "incomplete queue should stay pending");
}

/* ---- Finding 1: atomic fragment header overwrite ---- */
static void test_atomic_overwrite(Slirp *s)
{
    /* UDP atomic fragment with a source port whose high byte is 0x99,
     * deliberately different from the next-header value 17. If the code read
     * fh->nxt after the memmove, ip_nh would be 0x99 and dispatch fail. */
    unsigned udp_len = 8 + 16;
    struct mbuf *m = m_get(s);
    m_inc(m, 40 + 8 + (int)udp_len);
    struct ip6 *h = mtod(m, struct ip6 *);
    set_v6hdr(h, (uint16_t)(8 + udp_len), IPPROTO_FRAGMENT, 237);
    uint8_t *p = (uint8_t *)m->m_data + 40;
    p[0] = IPPROTO_UDP;
    p[1] = 0;
    uint16_t of = htons(0); /* offset 0, M=0: atomic */
    memcpy(p + 2, &of, 2);
    uint32_t nid = htonl(0x12345);
    memcpy(p + 4, &nid, 4);
    uint8_t *udp = p + 8;
    uint16_t sport = htons(0x9999), dport = htons(7777), ulen = htons(udp_len);
    memcpy(udp, &sport, 2);
    memcpy(udp + 2, &dport, 2);
    memcpy(udp + 4, &ulen, 2);
    memset(udp + 6, 0, 2);
    for (int i = 0; i < 16; i++) {
        udp[8 + i] = (uint8_t)(i * 7 + 3);
    }
    m->m_len = 40 + 8 + (int)udp_len;

    struct mbuf *out = ip6_frag_input(s, m);
    CHECK(out != NULL, "atomic fragment must deliver");
    if (out) {
        struct ip6 *oh = mtod(out, struct ip6 *);
        CHECK(oh->ip_nh == IPPROTO_UDP,
              "atomic next-header must be 17, got %u (fh overwrite?)", oh->ip_nh);
        CHECK(ntohs(oh->ip_pl) == udp_len, "atomic payload length %u", ntohs(oh->ip_pl));
        CHECK(oh->ip_hl == 237, "atomic hop limit must be preserved, got %u",
              oh->ip_hl);
        uint8_t *udp2 = (uint8_t *)out->m_data + 40;
        uint16_t got_sport;
        memcpy(&got_sport, udp2, 2);
        CHECK(ntohs(got_sport) == 0x9999, "UDP source port preserved");
        m_free(out);
    }
}

/* ---- Finding 2: expiry with the 32-bit clock wrap ---- */
static void test_expiry(uint64_t uptime_ms)
{
    SlirpConfig cfg_dummy;
    (void)cfg_dummy;
    Slirp *s = make_slirp();
    /* slirp truncates the monotonic clock to 32-bit ms internally; keep the
     * full uptime in the virtual nanosecond clock so truncation happens the
     * same way production does. */
    g_now_ns = uptime_ms * 1000000ull;

    /* Fill the bounded queue table with incomplete datagrams. */
    for (uint32_t id = 1; id <= IP6_FRAG_MAX_QUEUES; id++) {
        make_live_queue(s, id);
    }
    CHECK(count_queues(s) == IP6_FRAG_MAX_QUEUES, "queues full at uptime %llu",
          (unsigned long long)uptime_ms);
    /* A 65th datagram is rejected while the table is full. */
    {
        uint8_t body[8] = {0};
        struct mbuf *m = make_frag(s, IPPROTO_UDP, 64, 999, 0, 1, 0, 0, body, 8);
        CHECK(ip6_frag_input(s, m) == NULL, "65th queue rejected");
    }
    CHECK(count_queues(s) == IP6_FRAG_MAX_QUEUES, "still full");

    /* Just before the 60 s TTL everything stays. */
    uint32_t cur = (uint32_t)uptime_ms;
    ip6_frag_slowtimo(s, cur + IP6_FRAG_TTL_MS - 1);
    CHECK(count_queues(s) == IP6_FRAG_MAX_QUEUES,
          "queues survive 59.999 s at uptime %llu",
          (unsigned long long)uptime_ms);

    /* At/after the TTL every queue is reaped and capacity recovers. */
    ip6_frag_slowtimo(s, cur + IP6_FRAG_TTL_MS);
    CHECK(count_queues(s) == 0, "all queues expire at 60 s near uptime %llu",
          (unsigned long long)uptime_ms);

    /* The table can be refilled immediately without restarting; advance the
     * virtual clock first so new deadlines are strictly in the future. */
    g_now_ns = (uint64_t)(uptime_ms + IP6_FRAG_TTL_MS) * 1000000ull;
    for (uint32_t id = 1000; id < 1000 + IP6_FRAG_MAX_QUEUES; id++) {
        make_live_queue(s, id);
    }
    CHECK(count_queues(s) == IP6_FRAG_MAX_QUEUES, "capacity recovered");
    ip6_frag_slowtimo(s, uptime_ms + IP6_FRAG_TTL_MS);
    /* Refilled queues were created at the advanced deadline, so they stay. */
    CHECK(count_queues(s) == IP6_FRAG_MAX_QUEUES, "fresh queues not over-reaped");
    slirp_cleanup(s);
}

/* Deadline that itself wraps past 2^32 ms. */
static void test_expiry_wrap(void)
{
    Slirp *s = make_slirp();
    uint32_t uptime = 0xFFFFFFFFu - 1000; /* 1 s before the 32-bit rollover */
    g_now_ns = (uint64_t)uptime * 1000000ull;
    make_live_queue(s, 77);
    /* deadline = uptime + 60000 wraps to a small value. */
    ip6_frag_slowtimo(s, 0x0000EA00u); /* well after wrap, past the deadline */
    CHECK(count_queues(s) == 0, "expiry correct across the 2^32 wrap");
    slirp_cleanup(s);
}

/* ---- Finding 3: 65535/65536 boundary and max UDP ---- */
static void feed_full_size(Slirp *s, uint32_t id, unsigned total)
{
    unsigned off = 0;
    while (off < total) {
        unsigned chunk = total - off;
        int more;
        if (chunk > 8) {
            chunk = 8;
        }
        more = (off + chunk < total);
        uint8_t body[8];
        for (unsigned i = 0; i < chunk; i++) {
            body[i] = (uint8_t)((off + i) & 0xff);
        }
        struct mbuf *m = make_frag(s, IPPROTO_UDP, 128, id, off / 8, more, 0,
                                  0, body, chunk);
        struct mbuf *out = ip6_frag_input(s, m);
        if (off + chunk == total) {
            if (total == 65535) {
                CHECK(out != NULL, "65535 payload must reassemble");
                if (out) {
                    struct ip6 *oh = mtod(out, struct ip6 *);
                    CHECK(ntohs(oh->ip_pl) == 65535, "reassembled payload len");
                    CHECK(oh->ip_hl == 128, "first-fragment hop limit kept");
                    CHECK(oh->ip_nh == IPPROTO_UDP, "nxt from first fragment");
                    /* Spot-check reassembled bytes: pattern covers the full
                     * fragmentable part including the 8-byte UDP header. */
                    uint8_t *pl = (uint8_t *)out->m_data + 40;
                    CHECK(pl[8] == 8 && pl[65534] == (uint8_t)(65534 & 0xff),
                          "reassembled payload bytes consistent");
                    m_free(out);
                }
            } else {
                CHECK(out == NULL, "65536 payload must never reassemble");
            }
        } else {
            CHECK(out == NULL, "intermediate fragment stays pending");
        }
        off += chunk;
    }
}

static void test_size_bounds(void)
{
    Slirp *s = make_slirp();
    g_now_ns = 500000000000ull;
    feed_full_size(s, 0xA001, 65535);
    CHECK(count_queues(s) == 0, "65535 queue fully consumed");
    feed_full_size(s, 0xA002, 65536);
    /* The final over-long fragment is rejected; the queue must never
     * complete. Cleanup frees the bounded partial state. */
    slirp_cleanup(s);
}

/* ---- Finding 4: overlap / duplicate / terminal policy ---- */
static void test_exact_duplicate_keeps_queue(void)
{
    Slirp *s = make_slirp();
    g_now_ns = 123456789ull;
    uint8_t a[8] = {10, 11, 12, 13, 14, 15, 16, 17};
    uint8_t z[1] = {0};
    /* first fragment, M=1 */
    struct mbuf *m1 = make_frag(s, IPPROTO_UDP, 64, 1, 0, 1, 0, 0, a, 8);
    CHECK(ip6_frag_input(s, m1) == NULL, "first pending");
    /* exact duplicate of the first fragment: ignored, queue retained */
    struct mbuf *m1b = make_frag(s, IPPROTO_UDP, 64, 1, 0, 1, 0, 0, a, 8);
    CHECK(ip6_frag_input(s, m1b) == NULL, "duplicate ignored");
    CHECK(count_queues(s) == 1, "queue retained after exact duplicate");
    /* terminal completes it */
    struct mbuf *m2 = make_frag(s, IPPROTO_UDP, 64, 1, 1, 0, 0, 0, z, 1);
    struct mbuf *out = ip6_frag_input(s, m2);
    CHECK(out != NULL, "datagram still completes after duplicate");
    if (out) {
        m_free(out);
    }
    slirp_cleanup(s);
}

static void test_partial_overlap_discards(void)
{
    Slirp *s = make_slirp();
    g_now_ns = 123456789ull;
    uint8_t a[16];
    for (int i = 0; i < 16; i++) a[i] = (uint8_t)i;
    /* First fragment covers [0,16). */
    struct mbuf *m1 = make_frag(s, IPPROTO_UDP, 64, 2, 0, 1, 0, 0, a, 16);
    CHECK(ip6_frag_input(s, m1) == NULL, "first pending");
    /* Second fragment covers [8,24): overlapping bytes happen to MATCH, but it
     * is not an exact duplicate - RFC 8200 requires discarding the queue. */
    uint8_t b[16];
    for (int i = 0; i < 16; i++) b[i] = a[(8 + i) & 15];
    struct mbuf *m2 = make_frag(s, IPPROTO_UDP, 64, 2, 1, 1, 0, 0, b, 16);
    CHECK(ip6_frag_input(s, m2) == NULL, "partial overlap consumed");
    CHECK(count_queues(s) == 0, "queue discarded on partial overlap");
    /* A later clean datagram with the same id is not poisoned. */
    uint8_t c[8] = {1};
    struct mbuf *m3 = make_frag(s, IPPROTO_UDP, 64, 2, 0, 1, 0, 0, c, 8);
    CHECK(ip6_frag_input(s, m3) == NULL, "clean first accepted");
    CHECK(count_queues(s) == 1, "fresh queue works after discard");
    slirp_cleanup(s);
}

static void test_conflicting_first_discards(void)
{
    Slirp *s = make_slirp();
    g_now_ns = 123456789ull;
    uint8_t a[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    uint8_t b[8] = {9, 9, 9, 9, 9, 9, 9, 9};
    struct mbuf *m1 = make_frag(s, IPPROTO_UDP, 64, 3, 0, 1, 0, 0, a, 8);
    CHECK(ip6_frag_input(s, m1) == NULL, "first pending");
    struct mbuf *m2 = make_frag(s, IPPROTO_UDP, 64, 3, 0, 1, 0, 0, b, 8);
    CHECK(ip6_frag_input(s, m2) == NULL, "conflicting first consumed");
    CHECK(count_queues(s) == 0, "old queue torn down by conflicting first");
    slirp_cleanup(s);
}

static void test_inconsistent_terminals(void)
{
    Slirp *s = make_slirp();
    g_now_ns = 123456789ull;
    uint8_t a[8] = {1};
    /* first fragment [0,8), M=1 */
    struct mbuf *m1 = make_frag(s, IPPROTO_UDP, 64, 4, 0, 1, 0, 0, a, 8);
    ip6_frag_input(s, m1);
    /* terminal at off 16 with a gap still open -> queue stays incomplete */
    struct mbuf *m2 = make_frag(s, IPPROTO_UDP, 64, 4, 2, 0, 0, 0, a, 8);
    CHECK(ip6_frag_input(s, m2) == NULL, "early terminal leaves queue pending");
    CHECK(count_queues(s) == 1, "one incomplete queue");
    /* another terminal at the same offset with different length -> discard */
    uint8_t b[4] = {2, 2, 2, 2};
    struct mbuf *m3 = make_frag(s, IPPROTO_UDP, 64, 4, 2, 0, 0, 0, b, 4);
    CHECK(ip6_frag_input(s, m3) == NULL, "conflicting terminal consumed");
    CHECK(count_queues(s) == 0, "queue discarded on terminal mismatch");
    slirp_cleanup(s);
}

/* ---- Finding 5: reserved byte/bits ignored ---- */
static void test_reserved_accepted(void)
{
    Slirp *s = make_slirp();
    g_now_ns = 123456789ull;
    uint8_t a[8] = {5, 5, 5, 5, 5, 5, 5, 5};
    uint8_t z[1] = {0};
    /* reserved flag bits = 0b10 and reserved byte = 0xAB: must be accepted. */
    struct mbuf *m1 = make_frag(s, IPPROTO_UDP, 64, 5, 0, 1, 0xAB, 0x2, a, 8);
    CHECK(ip6_frag_input(s, m1) == NULL, "nonzero reserved first accepted");
    CHECK(count_queues(s) == 1, "queue exists despite reserved bits");
    struct mbuf *m2 = make_frag(s, IPPROTO_UDP, 64, 5, 1, 0, 0xAB, 0x2, z, 1);
    struct mbuf *out = ip6_frag_input(s, m2);
    CHECK(out != NULL, "nonzero reserved datagram reassembles");
    if (out) {
        m_free(out);
    }
    slirp_cleanup(s);
}

static void *test_timer_new_stub(SlirpTimerCb cb, void *o, void *x)
{
    (void)cb;
    (void)o;
    (void)x;
    static int token;
    return &token;
}
static void test_timer_free_stub(void *t, void *o)
{
    (void)t;
    (void)o;
}
static void test_timer_mod_stub(void *t, int64_t e, void *o)
{
    (void)t;
    (void)e;
    (void)o;
}

int main(void)
{
    /* Finding 1 */
    {
        Slirp *s = make_slirp();
        g_now_ns = 420000000000ull;
        test_atomic_overwrite(s);
        slirp_cleanup(s);
    }
    /* Finding 2: uptime at 2^32 ms, 3*2^32 ms, and a deadline crossing wrap. */
    test_expiry(1ull << 32);
    test_expiry(3ull << 32);
    test_expiry_wrap();
    /* Finding 3 */
    test_size_bounds();
    /* Finding 4 */
    test_exact_duplicate_keeps_queue();
    test_partial_overlap_discards();
    test_conflicting_first_discards();
    test_inconsistent_terminals();
    /* Finding 5 */
    test_reserved_accepted();

    if (failures) {
        printf("NATIVE IP6 FRAG TESTS: %d FAILURE(S)\n", failures);
        return 1;
    }
    printf("NATIVE IP6 FRAG TESTS: ALL PASS\n");
    return 0;
}
