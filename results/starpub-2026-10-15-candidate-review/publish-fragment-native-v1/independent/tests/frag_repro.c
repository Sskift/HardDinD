/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Independent regression reproducer for HardDinD IPv6 fragmentation backport
 * on pinned libslirp 4.6.1 (patch 0001-ipv6-fragmentation, frozen candidate).
 *
 * SCOPE / METHOD
 *  - Directly calls the ACTUAL production functions ip6_frag_input() and
 *    ip6_frag_slowtimo() from the compiled static library (no reimplementation
 *    of their logic) through their real mbuf/Slirp data structures.
 *  - Clock is injected via the real SlirpCb.clock_get_ns callback (no real
 *    sleep). slowtimo_call() reproduces the exact production argument
 *    computation at slirp.c:689/702 (curtime is 'unsigned', i.e. the 64-bit
 *    CLOCK_MONOTONIC-ms value truncated to 32 bits). When built with
 *    -DFRAG_SLOWTIMO_64 against a fixed archive, the full 64-bit value is
 *    passed instead.
 *  - This is NOT a guest end-to-end/Docker test: ip6_input() dispatch, the
 *    slirp_pollfds_poll() timer gate and socket layers are not exercised.
 *
 * Exit status: 0 iff every RFC-correct expectation holds; nonzero otherwise
 * (frozen candidate is expected to FAIL the bug cases, preserving evidence).
 */
#include "slirp.h"
#include "ip6_frag.h"

#include <glib.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LINK_HDR sizeof(struct ip6)            /* 40 */
#define FRAG_HDR 8
#define UDP_PROTO IPPROTO_UDP                  /* 17 */
#define TCP_PROTO IPPROTO_TCP                  /* 6 */
#define FRAG_PROTO IPPROTO_FRAGMENT            /* 44 */

static uint64_t g_clock_ns;
static int64_t fake_clock_get_ns(void *opaque)
{
    (void)opaque;
    return (int64_t)g_clock_ns;
}

static const SlirpCb g_cbs = {
    .clock_get_ns = fake_clock_get_ns,
};

static Slirp *new_slirp(void)
{
    Slirp *s = g_new0(Slirp, 1);
    s->if_mtu = 1500;
    s->if_mru = 1500;
    m_init(s);
    /* if_init() body: make the interface queue heads walkable so m_cleanup()
     * can run in teardown (the packet path under test never touches them). */
    s->if_fastq.qh_link = s->if_fastq.qh_rlink = &s->if_fastq;
    s->if_batchq.qh_link = s->if_batchq.qh_rlink = &s->if_batchq;
    ip6_frag_init(s);
    s->cb = &g_cbs;
    s->opaque = NULL;
    g_clock_ns = 0;
    return s;
}

static void destroy_slirp(Slirp *s)
{
    ip6_frag_cleanup(s);
    m_cleanup(s);
    g_free(s);
}

static void set_clock_ms(uint64_t ms)
{
    g_clock_ns = ms * 1000000ULL;
}

/* Mirrors slirp.c:689: unsigned curtime = clock_get_ns()/SCALE_MS; */
static void slowtimo_call(Slirp *s)
{
#ifdef FRAG_SLOWTIMO_64
    ip6_frag_slowtimo(s, (uint64_t)(g_clock_ns / 1000000ULL));
#else
    unsigned curtime_ms = (unsigned)(g_clock_ns / 1000000ULL);
    ip6_frag_slowtimo(s, curtime_ms);
#endif
}

static unsigned queue_count(Slirp *s)
{
    unsigned n = 0;
    for (struct ip6fq *q = s->ip6_reassq; q; q = q->next) {
        n++;
    }
    return n;
}

static uint64_t first_expire_ms(Slirp *s)
{
    return s->ip6_reassq ? s->ip6_reassq->expire_ms : 0;
}

/*
 * Wire-format packet builder. payload/off_flags/etc describe one received
 * IPv6 fragment packet. src/dst are fixed 16-byte addresses.
 */
static struct mbuf *build_frag(Slirp *s, uint8_t nxt, uint8_t res,
                               uint16_t off_flags, uint32_t id,
                               uint8_t hoplimit, const uint8_t *payload,
                               unsigned chunk, uint8_t src_last,
                               uint8_t dst_last)
{
    unsigned total = LINK_HDR + FRAG_HDR + chunk;
    struct mbuf *m = m_get(s);
    m_inc(m, (int)total);
    uint8_t *p = (uint8_t *)m->m_data;
    memset(p, 0, total);
    p[0] = 0x60;                                  /* version 6 */
    p[4] = (uint8_t)((total - LINK_HDR) >> 8);    /* payload length = 8+chunk */
    p[5] = (uint8_t)(total - LINK_HDR);
    p[6] = FRAG_PROTO;
    p[7] = hoplimit;
    p[23] = src_last;                             /* src ::last */
    p[39] = dst_last;                             /* dst ::last */
    p[40] = nxt;
    p[41] = res;
    p[42] = (uint8_t)(off_flags >> 8);
    p[43] = (uint8_t)off_flags;
    p[44] = (uint8_t)(id >> 24);
    p[45] = (uint8_t)(id >> 16);
    p[46] = (uint8_t)(id >> 8);
    p[47] = (uint8_t)id;
    if (chunk) {
        memcpy(p + 48, payload, chunk);
    }
    m->m_len = (int)total;
    return m;
}

static struct mbuf *first_frag(Slirp *s, uint32_t id, bool more,
                               const uint8_t *payload, unsigned chunk)
{
    uint16_t off_flags = more ? 0x0001 : 0x0000;
    return build_frag(s, UDP_PROTO, 0, off_flags, id, 64, payload, chunk, 1, 2);
}

static struct mbuf *frag_at(Slirp *s, uint32_t id, unsigned byte_off, bool more,
                            const uint8_t *payload, unsigned chunk)
{
    uint16_t off_flags = (uint16_t)((byte_off & 0xfff8) | (more ? 1 : 0));
    return build_frag(s, UDP_PROTO, 0, off_flags, id, 64, payload, chunk, 1, 2);
}

/* ---------- tiny test framework ---------- */

static int g_failures, g_checks, g_bugconf;
static const char *g_case;

static void CASE(const char *name)
{
    g_case = name;
    printf("# %s\n", name);
}

#define EXPECT(cond, fmt, ...)                                                          \
    do {                                                                                \
        g_checks++;                                                                     \
        if (!(cond)) {                                                                  \
            g_failures++;                                                               \
            printf("  FAIL [%s] " fmt "\n", g_case, ##__VA_ARGS__);                    \
        } else {                                                                        \
            printf("  ok   [%s] " fmt "\n", g_case, ##__VA_ARGS__);                    \
        }                                                                               \
    } while (0)

#define BUG(cond, fmt, ...)                                                             \
    do {                                                                                \
        g_checks++;                                                                     \
        if (cond) {                                                                     \
            g_bugconf++;                                                                \
            printf("  BUG-CONFIRMED [%s] " fmt "\n", g_case, ##__VA_ARGS__);           \
        } else {                                                                        \
            printf("  ok-not-buggy [%s] " fmt "\n", g_case, ##__VA_ARGS__);            \
        }                                                                               \
    } while (0)

/* T1/T2: atomic fragment must carry the fragment header's Next Header into
 * the reassembled fixed header. The memmove shifts payload over the exact
 * bytes of fh (m_data+40..47); reading fh->nxt afterwards reads payload[0]. */
static void test_atomic(const char *name, uint8_t inner_proto,
                        uint16_t src_port)
{
    Slirp *s = new_slirp();
    uint8_t payload[8];
    payload[0] = (uint8_t)(src_port >> 8);
    payload[1] = (uint8_t)src_port;
    payload[2] = 0x04;
    payload[3] = 0xd2; /* dst port 1234 */
    payload[4] = 0x00;
    payload[5] = 0x08;
    payload[6] = 0x12;
    payload[7] = 0x34;

    struct mbuf *m = build_frag(s, inner_proto, 0, 0x0000 /* off0, M=0 */, 1,
                                64, payload, sizeof(payload), 1, 2);
    struct mbuf *out = ip6_frag_input(s, m);
    CASE(name);
    BUG(out == NULL, "atomic fragment dropped");
    if (out) {
        struct ip6 *ip = mtod(out, struct ip6 *);
        printf("  observed ip_nh=%u (want %u); payload[0]=0x%02x\n", ip->ip_nh,
               inner_proto, (uint8_t)out->m_data[LINK_HDR]);
        BUG(ip->ip_nh == payload[0] && payload[0] != inner_proto,
            "ip_nh overwritten with first payload byte 0x%02x", payload[0]);
        EXPECT(ip->ip_nh == inner_proto, "fixed header ip_nh == %u",
               inner_proto);
        EXPECT(out->m_len == (int)LINK_HDR + (int)sizeof(payload),
               "m_len 40+8=%d (got %d)", (int)LINK_HDR + 8, out->m_len);
        uint16_t pl = (uint16_t)((uint8_t)out->m_data[4] << 8 |
                                 (uint8_t)out->m_data[5]);
        EXPECT(pl == sizeof(payload), "ip_pl == 8 (got %u)", pl);
        EXPECT(memcmp(out->m_data + LINK_HDR, payload, sizeof(payload)) == 0,
               "payload shifted intact");
        m_free(out);
    }
    destroy_slirp(s);
}

/* Expiry core: create one incomplete queue, advance clock past 60s TTL. */
static void test_expiry(const char *name, uint64_t t0_ms, int create_new_each)
{
    (void)create_new_each;
    Slirp *s = new_slirp();
    set_clock_ms(t0_ms);
    uint8_t pay[8] = {0};
    struct mbuf *m = first_frag(s, 7, true, pay, sizeof(pay));
    struct mbuf *r = ip6_frag_input(s, m);
    CASE(name);
    EXPECT(r == NULL && queue_count(s) == 1, "incomplete queue held at t0");
    uint64_t deadline = first_expire_ms(s);
    EXPECT(deadline == t0_ms + 60000,
           "expire_ms = full64 t0+60000 = %llu", (unsigned long long)deadline);

    set_clock_ms(t0_ms + 60001);
    unsigned truncated = (unsigned)((t0_ms + 60001) & 0xffffffffULL);
    printf("  production curtime (uint32) = %llu ; queue deadline (uint64) = %llu\n",
           (unsigned long long)truncated, (unsigned long long)deadline);
    slowtimo_call(s);
    BUG(t0_ms + 60000 > 0xffffffffULL && queue_count(s) == 1,
        "queue NOT reaped %llu ms past TTL: 32-bit curtime %llu < 64-bit deadline",
        (unsigned long long)60001ULL, (unsigned long long)truncated);
    EXPECT(queue_count(s) == 0,
           "queue reaped by slow timer after TTL (64-bit-consistent clock)");
    destroy_slirp(s);
}

/* Capacity must recover after TTL even when uptime > 2^32 ms. */
static void test_capacity_recovery(void)
{
    Slirp *s = new_slirp();
    uint64_t t0 = 13900000000ULL; /* ~160.9 days, like Starpub uptime */
    set_clock_ms(t0);
    uint8_t pay[8] = {0};
    CASE("expiry/capacity: 64 queues reclaim at >2^32 ms uptime");
    for (uint32_t id = 1000; id < 1064; id++) {
        struct mbuf *m = first_frag(s, id, true, pay, sizeof(pay));
        if (ip6_frag_input(s, m) != NULL) {
            g_failures++;
        }
    }
    EXPECT(queue_count(s) == 64, "64 queues filled (got %u)", queue_count(s));
    EXPECT(s->ip6_frag_count == 64, "64 fragments held (got %u)",
           s->ip6_frag_count);

    /* New flow while table is legitimately full is refused. */
    struct mbuf *m = first_frag(s, 1064, true, pay, sizeof(pay));
    EXPECT(ip6_frag_input(s, m) == NULL && queue_count(s) == 64,
           "65th flow refused while full");

    /* TTL elapses; the production slow timer uses the (truncated) curtime. */
    set_clock_ms(t0 + 60001);
    slowtimo_call(s);
    BUG(queue_count(s) == 64,
        "no reclaim past TTL at 161d uptime; reassembly capacity dead until restart");
    EXPECT(queue_count(s) == 0, "all stale queues reaped (got %u)",
           queue_count(s));
    EXPECT(s->ip6_frag_count == 0, "fragment counter back to 0 (got %u)",
           s->ip6_frag_count);
    struct mbuf *m2 = first_frag(s, 1065, true, pay, sizeof(pay));
    BUG(ip6_frag_input(s, m2) == NULL && queue_count(s) == 64,
        "new flow still refused after TTL (capacity never recovered)");

    /* Capacity recovered: a new flow is accepted and gets a FRESH 60s TTL.
     * On the frozen build m2 above was refused (table still full); on the
     * fixed build it was accepted as that fresh flow itself. */
#ifdef FRAG_SLOWTIMO_64
    EXPECT(queue_count(s) == 1 && s->ip6_frag_count == 1,
           "new flow id1065 accepted after reclaim (got %u queues)",
           queue_count(s));
    slowtimo_call(s); /* same instant: fresh deadline must survive */
    EXPECT(queue_count(s) == 1, "fresh queue kept before its own TTL (got %u)",
           queue_count(s));
    set_clock_ms(t0 + 120002);
    slowtimo_call(s);
    EXPECT(queue_count(s) == 0, "fresh queue reaped after its own 60s TTL");
#else
    struct mbuf *m3 = first_frag(s, 1066, true, pay, sizeof(pay));
    int accepted = ip6_frag_input(s, m3) == NULL && queue_count(s) == 1;
    EXPECT(accepted, "new flow accepted after reclaim (table stays wedged)");
    slowtimo_call(s);
    EXPECT(queue_count(s) == 0, "wedged table never reclaims (got %u)",
           queue_count(s));
#endif
    destroy_slirp(s);
}

/* Two-fragment datagram with total fragmentable payload == end. */
static int reassemble_end(Slirp *s, uint32_t id, unsigned end)
{
    uint8_t *p1 = g_malloc(8);
    memset(p1, 0xa5, 8);
    struct mbuf *a = first_frag(s, id, true, p1, 8);
    if (ip6_frag_input(s, a) != NULL) {
        g_free(p1);
        return -1;
    }
    unsigned c2 = end - 8;
    uint8_t *p2 = g_malloc(c2);
    memset(p2, 0x5a, c2);
    struct mbuf *b = frag_at(s, id, 8, false, p2, c2);
    struct mbuf *out = ip6_frag_input(s, b);
    g_free(p1);
    g_free(p2);
    if (!out) {
        return 0;
    }
    int ok = (out->m_len == (int)LINK_HDR + (int)end);
    uint16_t pl = (uint16_t)((uint8_t)out->m_data[4] << 8 |
                             (uint8_t)out->m_data[5]);
    ok = ok && pl == end;
    m_free(out);
    return ok ? 1 : -2;
}

static void test_max_payload(void)
{
    CASE("max payload: IPv6 Payload Length excludes the 40-byte header");
    struct {
        unsigned end;
        const char *desc;
    } cases[] = {
        {65495, "boundary 65535-40 (accepted today)"},
        {65496, "65496: legal but rejected by MAX_PAYLOAD=65495"},
        {65527 + 8, "65535 payload incl 8-byte UDP hdr (max UDP 65527)"},
        {65535, "max RFC 8200 payload 65535"},
        {65536, "65536: genuinely over the 16-bit Payload Length"},
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        Slirp *s = new_slirp();
        int r = reassemble_end(s, 2000 + i, cases[i].end);
        destroy_slirp(s);
        if (cases[i].end <= 65535) {
            BUG(r == 0, "%s -> dropped (end=%u)", cases[i].desc,
                cases[i].end);
            EXPECT(r == 1, "%s -> reassembled", cases[i].desc);
        } else {
            EXPECT(r == 0, "%s -> dropped", cases[i].desc);
        }
    }
}

/* Partial identical overlap (not an exact duplicate) must discard the whole
 * reassembly per RFC 8200 section 4.5; it must not stall as a zombie queue. */
static void test_partial_identical_overlap(void)
{
    Slirp *s = new_slirp();
    CASE("overlap: partial identical overlap discards whole queue");
    uint8_t a[16];
    for (unsigned i = 0; i < 16; i++) {
        a[i] = (uint8_t)i;
    }
    struct mbuf *ma = first_frag(s, 900, true, a, 16);
    EXPECT(ip6_frag_input(s, ma) == NULL && queue_count(s) == 1,
           "frag A off0 len16 M=1 queued");

    uint8_t b[16];
    for (unsigned i = 0; i < 16; i++) {
        b[i] = (uint8_t)(i + 8); /* bytes 8..15 identical, 16..23 new */
    }
    struct mbuf *mb = frag_at(s, 900, 8, false, b, 16);
    struct mbuf *r = ip6_frag_input(s, mb);
    BUG(r == NULL && queue_count(s) == 1 && s->ip6_frag_count == 2,
        "non-exact identical overlap kept; contiguity can never close (zombie)");
    EXPECT(r == NULL && queue_count(s) == 0,
           "whole datagram discarded on partial overlap");
    destroy_slirp(s);
}

/* Conflicting first fragment (same off/len, different bytes): RFC 8200
 * overlap rule destroys the whole datagram; the stale first fragment must
 * not survive and reassemble later. */
static void test_conflicting_first(void)
{
    Slirp *s = new_slirp();
    CASE("overlap: conflicting first fragment destroys stale queue");
    uint8_t a[16], ap[16], c[16];
    memset(a, 0x11, sizeof(a));
    memset(ap, 0x22, sizeof(ap));
    memset(c, 0x33, sizeof(c));
    struct mbuf *ma = first_frag(s, 901, true, a, 16);
    ip6_frag_input(s, ma);
    struct mbuf *mconf = first_frag(s, 901, true, ap, 16);
    ip6_frag_input(s, mconf); /* frozen: goto drop leaves old queue alive */
    BUG(queue_count(s) == 1,
        "conflicting off0 fragment only dropped itself; old queue retained");
    struct mbuf *mc = frag_at(s, 901, 16, false, c, 16);
    struct mbuf *out = ip6_frag_input(s, mc);
    BUG(out != NULL, "stale queue still reassembles with the rejected first");
    /* After a correct whole-queue discard, C alone opens a normal incomplete
     * queue (off16, no first yet); it must not carry the rejected bytes. */
    int clean = out == NULL && queue_count(s) == 1 &&
                s->ip6_reassq->frags->off == 16 &&
                s->ip6_reassq->frags->len == 16 &&
                !s->ip6_reassq->have_first;
    EXPECT(clean, "no reassembly; only the later off16 fragment remains queued");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

/* Exact duplicate first fragment: RFC 8200 allows discarding the duplicate
 * and keeping the queue; completion must still work. */
static void test_exact_duplicate_first(void)
{
    Slirp *s = new_slirp();
    CASE("overlap: exact duplicate first fragment ignored, queue kept");
    uint8_t a[16], c[16];
    memset(a, 0x44, sizeof(a));
    memset(c, 0x55, sizeof(c));
    ip6_frag_input(s, first_frag(s, 902, true, a, 16));
    struct mbuf *dup = first_frag(s, 902, true, a, 16);
    EXPECT(ip6_frag_input(s, dup) == NULL && queue_count(s) == 1 &&
               s->ip6_frag_count == 1,
           "exact duplicate discarded, queue retained");
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 902, 16, false, c, 16));
    EXPECT(out != NULL, "datagram completes after exact duplicate");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

/* Control: content conflict in the middle must discard the queue today. */
static void test_conflicting_middle(void)
{
    Slirp *s = new_slirp();
    CASE("overlap control: content conflict already discards whole queue");
    uint8_t a[16], b[16];
    memset(a, 0x66, sizeof(a));
    memset(b, 0x77, sizeof(b));
    ip6_frag_input(s, first_frag(s, 903, true, a, 16));
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 903, 8, false, b, 16));
    EXPECT(out == NULL && queue_count(s) == 0 && s->ip6_frag_count == 0,
           "conflicting overlap tears queue down");
    destroy_slirp(s);
}

/* Two terminal fragments with different ends: RFC 8200 has exactly one
 * last fragment per datagram; a second M=0 changing the total length must be
 * rejected (defense-in-depth consistency check, cheap to enforce). */
static void test_multiple_terminals(void)
{
    Slirp *s = new_slirp();
    CASE("terminal: distinct last-fragment ends do not overwrite q->end");
    uint8_t a[8], b[8], c[8], d[8];
    memset(a, 0xaa, sizeof(a));
    memset(b, 0xbb, sizeof(b));
    memset(c, 0xcc, sizeof(c));
    memset(d, 0xdd, sizeof(d));
    ip6_frag_input(s, first_frag(s, 905, true, a, 8));         /* 0..8  M=1 */
    ip6_frag_input(s, frag_at(s, 905, 16, false, b, 8));       /* 16..24 M=0, end=24, gap 8..16 */
    struct mbuf *mc = frag_at(s, 905, 24, false, c, 8);        /* 24..32 M=0, end=32 */
    struct mbuf *r = ip6_frag_input(s, mc);
    BUG(r == NULL && queue_count(s) == 1,
        "second terminal silently overwrote q->end 24->32");
    EXPECT(r == NULL && queue_count(s) == 0,
           "datagram discarded when terminal end changes");
    struct mbuf *md = frag_at(s, 905, 8, true, d, 8); /* would close 0..32 */
    struct mbuf *out = ip6_frag_input(s, md);
    BUG(out != NULL, "datagram with two terminals reassembled to 32 bytes");
    if (out) {
        EXPECT(out->m_len == 40 + 32, "poisoned assembly length observed");
        m_free(out);
    }
    destroy_slirp(s);
}

/* RFC 8200 section 4.5: both reserved fields are "ignored on reception". */
static void test_reserved_ignored(void)
{
    Slirp *s = new_slirp();
    CASE("reserved: nonzero Res/res accepted (ignored on reception)");
    uint8_t payload[8] = {0x9c, 0x40, 0x04, 0xd2, 0x00, 0x08, 0x12, 0x34};
    struct mbuf *m = build_frag(s, UDP_PROTO, 0xab, 0x0006 /* Res=0b11,M=0 */,
                                3, 64, payload, sizeof(payload), 1, 2);
    struct mbuf *out = ip6_frag_input(s, m);
    BUG(out == NULL, "atomic fragment with nonzero reserved bits dropped");
    EXPECT(out != NULL, "packet accepted despite nonzero reserved fields");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

/* Reassembled fixed header must preserve the first fragment's Hop Limit. */
static void test_hoplimit(void)
{
    Slirp *s = new_slirp();
    CASE("hoplimit: reassembled packet keeps first fragment hop limit (37)");
    uint8_t a[8], b[8];
    memset(a, 0x88, sizeof(a));
    memset(b, 0x99, sizeof(b));
    struct mbuf *ma = build_frag(s, UDP_PROTO, 0, 0x0001, 4, 37, a, 8, 1, 2);
    ip6_frag_input(s, ma);
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 4, 8, false, b, 8));
    if (out) {
        uint8_t hl = (uint8_t)out->m_data[7];
        BUG(hl == 64, "hop limit invented as 64 instead of first fragment 37");
        EXPECT(hl == 37, "hop limit 37 preserved (got %u)", hl);
        m_free(out);
    } else {
        g_failures++;
        printf("  FAIL [%s] reassembly returned NULL\n", g_case);
    }
    destroy_slirp(s);
}


/* Boundary stub: the production ip6_frag_output() calls if_output(so, m) to
 * enqueue each finished fragment. We capture the mbufs here so the real
 * ip6_frag_output() code runs without the socket/interface machinery. */
static struct mbuf *g_emit[128];
static int g_nemit;
void if_output(struct socket *so, struct mbuf *m)
{
    (void)so;
    if (g_nemit < (int)(sizeof(g_emit) / sizeof(g_emit[0]))) {
        g_emit[g_nemit++] = m;
    } else {
        m_free(m);
    }
}

static void free_emitted(void)
{
    for (int i = 0; i < g_nemit; i++) {
        m_free(g_emit[i]);
    }
    g_nemit = 0;
}

static struct mbuf *build_unfrag(Slirp *s, unsigned plen, uint8_t fill)
{
    unsigned total = LINK_HDR + plen;
    struct mbuf *m = m_get(s);
    m_inc(m, (int)total);
    memset(m->m_data, 0, total);
    m->m_data[0] = 0x60;
    m->m_data[4] = (uint8_t)(plen >> 8);
    m->m_data[5] = (uint8_t)plen;
    m->m_data[6] = UDP_PROTO;
    m->m_data[7] = 64;
    memset(m->m_data + LINK_HDR, fill, plen);
    m->m_len = (int)total;
    return m;
}

/* Output path control (owner: 1500-MTU chunking floors 1452 to 1448; this is
 * correct and generated reserved bits must be zero). */
static void test_output_chunking(void)
{
    Slirp *s = new_slirp();
    CASE("output: packets <= MTU pass unfragmented");
    g_nemit = 0;
    struct mbuf *m0 = build_unfrag(s, 1460, 0x77);
    EXPECT(ip6_frag_output(s, NULL, m0) == 0 && g_nemit == 1,
           "40+1460=1500 -> 1 emitted (got %d)", g_nemit);
    EXPECT(g_emit[0]->m_len == 1500 && g_emit[0]->m_data[6] == UDP_PROTO,
           "pass-through keeps next-header 17");
    free_emitted();

    CASE("output: 1500+ payload chunks (1500-40-8)&~7 = 1448, not 1452");
    g_nemit = 0;
    struct mbuf *m1 = build_unfrag(s, 1500, 0x88);
    EXPECT(ip6_frag_output(s, NULL, m1) == 0 && g_nemit == 2,
           "40+1500 -> 2 fragments (got %d)", g_nemit);
    if (g_nemit == 2) {
        EXPECT(g_emit[0]->m_len == 40 + 8 + 1448,
               "first wire size 40+8+1448=1496 (got %d)", g_emit[0]->m_len);
        EXPECT(g_emit[1]->m_len == 40 + 8 + 52,
               "second wire size 40+8+52=100 (got %d)", g_emit[1]->m_len);
        uint8_t *f0 = (uint8_t *)g_emit[0]->m_data;
        uint8_t *f1 = (uint8_t *)g_emit[1]->m_data;
        uint16_t of0 = (uint16_t)((f0[42] << 8) | f0[43]);
        uint16_t of1 = (uint16_t)((f1[42] << 8) | f1[43]);
        EXPECT(f0[6] == FRAG_PROTO && f0[40] == UDP_PROTO && (of0 & 0xfff8) == 0,
               "frag0 nh=44 inner nxt=17 offset0 M=%u", of0 & 1);
        EXPECT((of1 & 0xfff8) == 1448 && (of1 & 1) == 0,
               "frag1 offset 1448 M=0 (of_flags=0x%04x)", of1);
        EXPECT(f0[41] == 0 && (of0 & 0x0006) == 0 && f1[41] == 0 &&
                   (of1 & 0x0006) == 0,
               "generated reserved fields are zero");
        EXPECT(f0[44] == f1[44] && f0[45] == f1[45] && f0[46] == f1[46] &&
                   f0[47] == f1[47],
               "fragments share one Identification");
        /* reconstructed payload equals the original */
        int same = 1;
        for (int i = 0; i < 1448; i++) {
            if (g_emit[0]->m_data[48 + i] != (char)0x88) {
                same = 0;
            }
        }
        for (int i = 0; i < 52; i++) {
            if (g_emit[1]->m_data[48 + i] != (char)0x88) {
                same = 0;
            }
        }
        EXPECT(same, "both chunk payloads match original");
    }
    free_emitted();

    CASE("output: max 65535 payload -> 46 fragments, all <= MTU");
    g_nemit = 0;
    struct mbuf *m2 = build_unfrag(s, 65535, 0x99);
    int rc = ip6_frag_output(s, NULL, m2);
    int allok = rc == 0 && g_nemit == 46;
    long cover = 0;
    for (int i = 0; i < g_nemit; i++) {
        if (g_emit[i]->m_len > s->if_mtu) {
            allok = 0;
        }
        uint8_t *fp = (uint8_t *)g_emit[i]->m_data;
        uint16_t of = (uint16_t)((fp[42] << 8) | fp[43]);
        int chunk = g_emit[i]->m_len - 48;
        if (i < 45) {
            if (chunk != 1448 || (of & 1) != 1) {
                allok = 0;
            }
        } else {
            if (chunk != 375 || (of & 1) != 0) {
                allok = 0;
            }
        }
        cover += chunk;
    }
    EXPECT(allok && cover == 65535,
           "46 frags: 45x1448 M=1 then 375 M=0, coverage=%ld (emitted=%d)",
           cover, g_nemit);
    free_emitted();
    destroy_slirp(s);
}

int main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("== HardDinD ip6_frag direct-function reproducer "
#ifdef FRAG_SLOWTIMO_64
           "(64-bit-slowtimo build)\n"
#else
           "(frozen/production 32-bit-curtime build)\n"
#endif
        );
    FILE *up = fopen("/proc/uptime", "r");
    if (up) {
        char buf[128];
        if (fgets(buf, sizeof(buf), up)) {
            printf("   host /proc/uptime: %s", buf);
        }
        fclose(up);
    }
    printf("   native clock rollover: 2^32 ms = %.2f days\n",
           4294967296.0 / 1000.0 / 86400.0);

    test_atomic("atomic fragment UDP srcport=40000 (0x9c40), nxt=17",
                UDP_PROTO, 40000);
    test_atomic("atomic fragment TCP srcport=40000 (0x9c40), nxt=6",
                TCP_PROTO, 40000);
    test_atomic("atomic fragment UDP srcport=4353 (0x1101), nxt=17 (accidental match)",
                UDP_PROTO, 4353);

    test_expiry("expiry pre-wrap: t0=5,000,000 ms (32-bit curtime consistent)",
                5000000ULL, 0);
    test_expiry("expiry around 2^32: t0=2^32-30000, deadline crosses wrap",
                4294967296ULL - 30000, 0);
    test_expiry("expiry at Starpub-like uptime: t0=13,900,000,000 ms (~161d)",
                13900000000ULL, 0);
    test_capacity_recovery();

    test_max_payload();
    test_partial_identical_overlap();
    test_conflicting_first();
    test_exact_duplicate_first();
    test_conflicting_middle();
    test_multiple_terminals();
    test_reserved_ignored();
    test_hoplimit();
    test_output_chunking();

    printf("SUMMARY checks=%d failures=%d bug_conditions_observed=%d\n",
           g_checks, g_failures, g_bugconf);
    return g_failures ? 1 : 0;
}
