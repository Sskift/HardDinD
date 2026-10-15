/* SPDX-License-Identifier: BSD-3-Clause
 *
 * v2 portable BEHAVIORAL regression harness for the HardDinD IPv6
 * fragmentation backport on pinned libslirp 4.6.1.
 *
 * Scope (unchanged from v1): this links the compiled static library and
 * calls the ACTUAL production functions ip6_frag_input()/ip6_frag_slowtimo()
 * (and ip6_frag_output() across an if_output() boundary stub) on real Slirp/
 * mbuf structures. It is NOT a guest/Docker/L2 end-to-end test.
 *
 * v2 clock contract (this is the v1 harness-bug fix):
 *  - There is exactly ONE production clock path: slirp.c stores
 *    `unsigned curtime = clock_get_ns()/SCALE_MS;` (32-bit truncation) and
 *    calls ip6_frag_slowtimo(slirp, curtime). This harness reproduces that
 *    exact call with the production-width argument for EVERY candidate:
 *        uint32_t curtime = (uint32_t)(clock_ns / 1000000);
 *    No FRAG_SLOWTIMO_64 macro, no widened call ABI, no representation
 *    assumptions: the queue's internal expire_ms field is never read.
 *  - TTL semantics are asserted purely as externally observable behavior on
 *    the injected monotonic timeline ("before TTL still queued; at/after TTL
 *    reaped; a queue created after reclaim gets a FRESH TTL"), so the same
 *    binary test is correct for both a consistent-64-bit implementation and
 *    the supported wrap-safe-32-bit implementation.
 *  - No real sleep: the monotonic ns clock is injected.
 *
 * Exit 0 iff all behavioral expectations hold; nonzero otherwise. The
 * original buggy candidate is expected to fail the wrap/rollover cases.
 */
#include "slirp.h"
#include "ip6_frag.h"

#include <glib.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LINK_HDR 40
#define FRAG_HDR 8
#define UDP_PROTO IPPROTO_UDP
#define TCP_PROTO IPPROTO_TCP
#define FRAG_PROTO IPPROTO_FRAGMENT
#define TTL_MS 60000u

static uint64_t g_clock_ns;
static int64_t fake_clock_get_ns(void *opaque)
{
    (void)opaque;
    return (int64_t)g_clock_ns;
}

static const SlirpCb g_cbs = { .clock_get_ns = fake_clock_get_ns };

static Slirp *new_slirp(void)
{
    Slirp *s = g_new0(Slirp, 1);
    s->if_mtu = 1500;
    s->if_mru = 1500;
    m_init(s);
    s->if_fastq.qh_link = s->if_fastq.qh_rlink = &s->if_fastq;
    s->if_batchq.qh_link = s->if_batchq.qh_rlink = &s->if_batchq;
    ip6_frag_init(s);
    s->cb = &g_cbs;
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

/* EXACT production call: slirp.c:689 truncates ns/ms into the file-scope
 * `unsigned curtime`; slirp.c:702 passes it to ip6_frag_slowtimo(). */
static void slowtimo_call(Slirp *s)
{
    uint32_t curtime_ms = (uint32_t)(g_clock_ns / 1000000ULL);
    ip6_frag_slowtimo(s, curtime_ms);
}

static unsigned queue_count(Slirp *s)
{
    unsigned n = 0;
    for (struct ip6fq *q = s->ip6_reassq; q; q = q->next) {
        n++;
    }
    return n;
}

static const char *g_case;
static int g_checks, g_failures, g_bugconf;

static void CASE(const char *name)
{
    g_case = name;
    printf("# %s\n", name);
}

#define EXPECT(cond, fmt, ...)                                                        \
    do {                                                                              \
        g_checks++;                                                                   \
        if (!(cond)) {                                                                \
            g_failures++;                                                             \
            printf("  FAIL [%s] " fmt "\n", g_case, ##__VA_ARGS__);                  \
        } else {                                                                      \
            printf("  ok   [%s] " fmt "\n", g_case, ##__VA_ARGS__);                  \
        }                                                                             \
    } while (0)

/* BUG = candidate defect observed (the harness expectation is right). On the
 * original buggy build these fire at rollover; on a correct build they don't. */
#define BUG(cond, fmt, ...)                                                           \
    do {                                                                              \
        g_checks++;                                                                   \
        if (cond) {                                                                   \
            g_bugconf++;                                                              \
            printf("  BUG-CONFIRMED [%s] " fmt "\n", g_case, ##__VA_ARGS__);         \
        } else {                                                                      \
            printf("  ok-not-buggy [%s] " fmt "\n", g_case, ##__VA_ARGS__);          \
        }                                                                             \
    } while (0)

/* ------------------------- wire packet builders ------------------------- */

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
    p[0] = 0x60;
    p[4] = (uint8_t)((total - LINK_HDR) >> 8);
    p[5] = (uint8_t)(total - LINK_HDR);
    p[6] = FRAG_PROTO;
    p[7] = hoplimit;
    p[23] = src_last;
    p[39] = dst_last;
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
    return build_frag(s, UDP_PROTO, 0, more ? 0x0001 : 0x0000, id, 64,
                      payload, chunk, 1, 2);
}

static struct mbuf *frag_at(Slirp *s, uint32_t id, unsigned byte_off, bool more,
                            const uint8_t *payload, unsigned chunk)
{
    uint16_t off_flags = (uint16_t)((byte_off & 0xfff8) | (more ? 1 : 0));
    return build_frag(s, UDP_PROTO, 0, off_flags, id, 64, payload, chunk, 1, 2);
}

/* Submit one incomplete first fragment (returns queue presence). */
static int plant_queue(Slirp *s, uint32_t id)
{
    static const uint8_t pay[8] = {0};
    struct mbuf *m = first_frag(s, id, true, pay, sizeof(pay));
    struct mbuf *r = ip6_frag_input(s, m);
    return r == NULL && queue_count(s) >= 1;
}

/* --------------------- behavioral TTL/rollover tests --------------------- */

/* Pure behavior: a queue created at t0 must survive until 59,999 ms and be
 * reaped at 60,000 ms, regardless of 32-bit truncation/rollover. */
static void test_ttl_at(const char *label, uint64_t t0)
{
    Slirp *s = new_slirp();
    CASE(label);
    set_clock_ms(t0);
    EXPECT(plant_queue(s, 7), "incomplete queue held at t0=%llu",
           (unsigned long long)t0);
    printf("  (uint32 clock: t0->%u, t0+59999->%u, t0+60000->%u)\n",
           (uint32_t)t0, (uint32_t)(t0 + 59999), (uint32_t)(t0 + 60000));

    set_clock_ms(t0 + TTL_MS - 1);
    slowtimo_call(s);
    BUG(queue_count(s) == 0, "queue reaped 1 ms BEFORE its 60s TTL (early)");
    EXPECT(queue_count(s) == 1, "queue survives at t0+59999");

    set_clock_ms(t0 + TTL_MS);
    slowtimo_call(s);
    BUG(queue_count(s) == 1,
        "queue NOT reaped at t0+60000 (32-bit wrapping clock)");
    EXPECT(queue_count(s) == 0, "queue reaped exactly at t0+60000");
    destroy_slirp(s);
}

/* Per-queue deadlines: two queues born 30 s apart expire 30 s apart. */
static void test_independent_deadlines(void)
{
    Slirp *s = new_slirp();
    uint64_t t0 = 13900000000ULL; /* ~161 d, past rollover */
    CASE("ttl: per-queue deadlines across rollover (qA t0, qB t0+30s)");
    set_clock_ms(t0);
    EXPECT(plant_queue(s, 11), "qA queued");
    set_clock_ms(t0 + 30000);
    EXPECT(plant_queue(s, 12) && queue_count(s) == 2, "qB queued (2 held)");

    set_clock_ms(t0 + 59999);
    slowtimo_call(s);
    EXPECT(queue_count(s) == 2, "both survive 1 ms before qA TTL");
    set_clock_ms(t0 + 60000);
    slowtimo_call(s);
    BUG(queue_count(s) == 2, "qA not reaped at its TTL (rollover)");
    EXPECT(queue_count(s) == 1, "qA reaped, qB still fresh");
    set_clock_ms(t0 + 89999);
    slowtimo_call(s);
    EXPECT(queue_count(s) == 1, "qB survives 1 ms before its TTL");
    set_clock_ms(t0 + 90000);
    slowtimo_call(s);
    BUG(queue_count(s) == 1, "qB not reaped at its TTL (rollover)");
    EXPECT(queue_count(s) == 0, "qB reaped; table empty");
    destroy_slirp(s);
}

/* Reclaim at high uptime must restore the fixed 64-queue capacity, and a flow
 * admitted after reclaim must get a FRESH 60 s TTL (v1 #else harness bug
 * assumed the wedged-table schedule). */
static void test_capacity_recovery(void)
{
    Slirp *s = new_slirp();
    uint64_t t0 = 13900000000ULL;
    static const uint8_t pay[8] = {0};
    CASE("capacity: 64-queue table reclaims past rollover and refills");

    set_clock_ms(t0);
    int filled = 1;
    for (uint32_t id = 1000; id < 1064; id++) {
        struct mbuf *m = first_frag(s, id, true, pay, sizeof(pay));
        if (ip6_frag_input(s, m) != NULL || queue_count(s) != (unsigned)(id - 999)) {
            filled = 0;
        }
    }
    EXPECT(filled && queue_count(s) == 64, "64 queues filled");
    EXPECT(s->ip6_frag_count == 64, "64 fragments held (got %u)",
           s->ip6_frag_count);
    struct mbuf *m = first_frag(s, 1064, true, pay, sizeof(pay));
    EXPECT(ip6_frag_input(s, m) == NULL && queue_count(s) == 64,
           "65th flow refused while table is legitimately full");

    set_clock_ms(t0 + TTL_MS - 1);
    slowtimo_call(s);
    BUG(queue_count(s) != 64, "queues reaped before TTL (early)");
    EXPECT(queue_count(s) == 64, "all 64 survive 1 ms before TTL");

    set_clock_ms(t0 + TTL_MS);
    slowtimo_call(s);
    BUG(queue_count(s) == 64,
        "no reclaim at 161d uptime: reassembly capacity dead until restart");
    EXPECT(queue_count(s) == 0, "all stale queues reaped after TTL");
    EXPECT(s->ip6_frag_count == 0, "fragment counter reset (got %u)",
           s->ip6_frag_count);
    if (queue_count(s) != 0) {
        printf("  SKIP dependent refill/fresh-TTL steps (reclaim failed)\n");
        destroy_slirp(s);
        return;
    }

    /* Refill without restart: capacity must be reusable. */
    int refilled = 1;
    for (uint32_t id = 2000; id < 2064; id++) {
        struct mbuf *mm = first_frag(s, id, true, pay, sizeof(pay));
        if (ip6_frag_input(s, mm) != NULL ||
            queue_count(s) != (unsigned)(id - 1999)) {
            refilled = 0;
        }
    }
    EXPECT(refilled && queue_count(s) == 64, "64 new flows fill reclaimed table");
    struct mbuf *m2 = first_frag(s, 2064, true, pay, sizeof(pay));
    EXPECT(ip6_frag_input(s, m2) == NULL && queue_count(s) == 64,
           "65th new flow refused while full");

    set_clock_ms(t0 + 2 * TTL_MS - 1);
    slowtimo_call(s);
    BUG(queue_count(s) != 64, "refilled queues reaped before their own TTL");
    EXPECT(queue_count(s) == 64, "refilled queues survive on their FRESH TTL");
    set_clock_ms(t0 + 2 * TTL_MS);
    slowtimo_call(s);
    EXPECT(queue_count(s) == 0, "refilled queues reaped at their own TTL");
    EXPECT(s->ip6_frag_count == 0, "counter reset after second cycle");
    destroy_slirp(s);
}

/* Fresh-TTL microcase mandated by the v1 review: one new flow born exactly at
 * reclaim time must live its own full 60 s, not inherit the old deadline. */
static void test_fresh_ttl_after_reclaim(void)
{
    Slirp *s = new_slirp();
    uint64_t t0 = 4294967296ULL - 30000; /* deadline straddles the first wrap */
    CASE("ttl: flow admitted at reclaim gets a fresh 60s across wrap");
    set_clock_ms(t0);
    EXPECT(plant_queue(s, 21), "old queue held");
    set_clock_ms(t0 + TTL_MS);
    slowtimo_call(s);
    BUG(queue_count(s) == 1, "old queue not reaped across wrap");
    EXPECT(queue_count(s) == 0, "old queue reaped");

    EXPECT(plant_queue(s, 22), "new queue admitted at reclaim instant");
    set_clock_ms(t0 + TTL_MS + TTL_MS - 1);
    slowtimo_call(s);
    BUG(queue_count(s) == 0, "new queue reaped on the OLD deadline");
    EXPECT(queue_count(s) == 1, "new queue survives on its fresh TTL");
    set_clock_ms(t0 + 2 * TTL_MS);
    slowtimo_call(s);
    EXPECT(queue_count(s) == 0, "new queue reaped at its own TTL");
    destroy_slirp(s);
}

/* ---------------------- finding 1: atomic fragment ---------------------- */

static void test_atomic(const char *name, uint8_t inner_proto,
                        uint16_t src_port)
{
    Slirp *s = new_slirp();
    uint8_t payload[8];
    payload[0] = (uint8_t)(src_port >> 8);
    payload[1] = (uint8_t)src_port;
    payload[2] = 0x04;
    payload[3] = 0xd2;
    payload[4] = 0x00;
    payload[5] = 0x08;
    payload[6] = 0x12;
    payload[7] = 0x34;

    struct mbuf *m = build_frag(s, inner_proto, 0, 0x0000, 1, 64, payload,
                                sizeof(payload), 1, 2);
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
        EXPECT((uint8_t)out->m_data[7] == 64, "atomic hop limit 64 preserved");
        EXPECT(out->m_len == LINK_HDR + (int)sizeof(payload),
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

/* ----------------------- finding 3: max payload ------------------------- */

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
    int ok = out->m_len == (int)LINK_HDR + (int)end;
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
        {65495, "boundary 65535-40 (old limit)"},
        {65496, "65496: legal, rejected by the old MAX_PAYLOAD=65495"},
        {65535, "max RFC 8200 fragmentable payload 65535 (max UDP 65527)"},
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

/* ------------------------ finding 4: overlaps --------------------------- */

static void test_partial_identical_overlap(void)
{
    Slirp *s = new_slirp();
    CASE("overlap: partial identical overlap discards whole queue");
    uint8_t a[16], b[16];
    for (unsigned i = 0; i < 16; i++) {
        a[i] = (uint8_t)i;
        b[i] = (uint8_t)(i + 8); /* 8..15 identical, 16..23 new */
    }
    struct mbuf *ma = first_frag(s, 900, true, a, 16);
    EXPECT(ip6_frag_input(s, ma) == NULL && queue_count(s) == 1,
           "frag A off0 len16 M=1 queued");
    struct mbuf *mb = frag_at(s, 900, 8, false, b, 16);
    struct mbuf *r = ip6_frag_input(s, mb);
    BUG(r == NULL && queue_count(s) == 1 && s->ip6_frag_count == 2,
        "non-exact identical overlap kept as a zombie queue");
    EXPECT(r == NULL && queue_count(s) == 0,
           "whole datagram discarded on partial overlap");
    destroy_slirp(s);
}

static void test_conflicting_first(void)
{
    Slirp *s = new_slirp();
    CASE("overlap: conflicting first fragment destroys stale queue");
    uint8_t a[16], ap[16], c[16];
    memset(a, 0x11, sizeof(a));
    memset(ap, 0x22, sizeof(ap));
    memset(c, 0x33, sizeof(c));
    ip6_frag_input(s, first_frag(s, 901, true, a, 16));
    ip6_frag_input(s, first_frag(s, 901, true, ap, 16));
    BUG(queue_count(s) == 1,
        "conflicting off0 fragment only dropped itself; old queue retained");
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 901, 16, false, c, 16));
    BUG(out != NULL, "stale queue still reassembles with the rejected first");
    EXPECT(out == NULL, "no reassembly after a conflicting first fragment");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

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
    EXPECT(out != NULL, "datagram completes after an exact duplicate");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

static void test_conflicting_middle(void)
{
    Slirp *s = new_slirp();
    CASE("overlap control: content conflict discards whole queue");
    uint8_t a[16], b[16];
    memset(a, 0x66, sizeof(a));
    memset(b, 0x77, sizeof(b));
    ip6_frag_input(s, first_frag(s, 903, true, a, 16));
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 903, 8, false, b, 16));
    EXPECT(out == NULL && queue_count(s) == 0 && s->ip6_frag_count == 0,
           "conflicting overlap tears queue down");
    destroy_slirp(s);
}

static void test_multiple_terminals(void)
{
    Slirp *s = new_slirp();
    CASE("terminal: distinct last-fragment ends do not overwrite q->end");
    uint8_t a[8], b[8], c[8], d[8];
    memset(a, 0xaa, 8);
    memset(b, 0xbb, 8);
    memset(c, 0xcc, 8);
    memset(d, 0xdd, 8);
    ip6_frag_input(s, first_frag(s, 905, true, a, 8));      /* 0..8  M=1 */
    ip6_frag_input(s, frag_at(s, 905, 16, false, b, 8));    /* 16..24 M=0 */
    struct mbuf *mc = frag_at(s, 905, 24, false, c, 8);     /* 24..32 M=0 */
    struct mbuf *r = ip6_frag_input(s, mc);
    BUG(r == NULL && queue_count(s) == 1,
        "second terminal silently overwrote q->end 24->32");
    EXPECT(r == NULL && queue_count(s) == 0,
           "datagram discarded when the terminal end changes");
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 905, 8, true, d, 8));
    BUG(out != NULL, "datagram with two terminals reassembled to 32 bytes");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

/* Exact retransmit of the terminal fragment while the queue is incomplete is
 * an exact duplicate (same off/len/M/data): retain queue; a conflicting
 * retransmit discards the whole datagram. Exercises the fixed code's
 * M-agreement classification for queued terminal fragments. */
static void test_duplicate_terminal(void)
{
    Slirp *s = new_slirp();
    CASE("overlap: exact retransmit of terminal fragment ignored, then completes");
    uint8_t a[8], b[8], c[8];
    memset(a, 0xaa, 8);
    memset(b, 0xbb, 8);
    memset(c, 0xcc, 8);
    ip6_frag_input(s, first_frag(s, 910, true, a, 8));       /* 0..8  M=1 */
    ip6_frag_input(s, frag_at(s, 910, 16, false, c, 8));     /* 16..24 M=0, gap */
    struct mbuf *dup = frag_at(s, 910, 16, false, c, 8);
    EXPECT(ip6_frag_input(s, dup) == NULL && queue_count(s) == 1 &&
               s->ip6_frag_count == 2,
           "identical terminal retransmit ignored (2 frags held)");
    struct mbuf *out = ip6_frag_input(s, frag_at(s, 910, 8, true, b, 8));
    EXPECT(out != NULL && out->m_len == 40 + 24,
           "gap fills after terminal retransmit; 24-byte datagram completes");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);

    s = new_slirp();
    CASE("overlap: conflicting terminal retransmit discards whole datagram");
    uint8_t c2[8];
    memset(c2, 0xdd, 8);
    ip6_frag_input(s, first_frag(s, 911, true, a, 8));
    ip6_frag_input(s, frag_at(s, 911, 16, false, c, 8));
    struct mbuf *bad = frag_at(s, 911, 16, false, c2, 8);
    struct mbuf *r = ip6_frag_input(s, bad);
    BUG(r == NULL && queue_count(s) == 1,
        "same-offset terminal with different bytes kept the queue");
    EXPECT(r == NULL && queue_count(s) == 0,
           "whole datagram discarded on conflicting terminal retransmit");
    destroy_slirp(s);
}

/* ------------------------ finding 5: reserved -------------------------- */

static void test_reserved_ignored(void)
{
    Slirp *s = new_slirp();
    CASE("reserved: nonzero Res/res accepted (ignored on reception)");
    uint8_t payload[8] = {0x9c, 0x40, 0x04, 0xd2, 0x00, 0x08, 0x12, 0x34};
    struct mbuf *m = build_frag(s, UDP_PROTO, 0xab, 0x0006, 3, 64, payload,
                                sizeof(payload), 1, 2);
    struct mbuf *out = ip6_frag_input(s, m);
    BUG(out == NULL, "atomic fragment with nonzero reserved bits dropped");
    EXPECT(out != NULL, "packet accepted despite nonzero reserved fields");
    if (out) {
        m_free(out);
    }
    destroy_slirp(s);
}

/* ------------------------ hop limit preservation ----------------------- */

static void test_hoplimit(void)
{
    Slirp *s = new_slirp();
    CASE("hoplimit: reassembled packet keeps first fragment hop limit (37)");
    uint8_t a[8], b[8];
    memset(a, 0x88, 8);
    memset(b, 0x99, 8);
    ip6_frag_input(s, build_frag(s, UDP_PROTO, 0, 0x0001, 4, 37, a, 8, 1, 2));
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

/* -------------------- ip6_frag_output boundary stub --------------------- */

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
        int same = 1;
        for (int i = 0; i < 1448; i++) {
            same &= (g_emit[0]->m_data[48 + i] == (char)0x88);
        }
        for (int i = 0; i < 52; i++) {
            same &= (g_emit[1]->m_data[48 + i] == (char)0x88);
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
            allok &= (chunk == 1448 && (of & 1) == 1);
        } else {
            allok &= (chunk == 375 && (of & 1) == 0);
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
    printf("== HardDinD ip6_frag v2 portable behavioral harness "
           "(production 32-bit-curtime ABI, no 64-bit variant)\n");
    FILE *up = fopen("/proc/uptime", "r");
    if (up) {
        char buf[128];
        if (fgets(buf, sizeof(buf), up)) {
            printf("   host /proc/uptime: %s", buf);
        }
        fclose(up);
    }
    printf("   2^32 ms = %.2f days; cases: pre-wrap, crossing-wrap, "
           "actual ~161d uptime (3.24 wraps), 10 wraps\n",
           4294967296.0 / 1000.0 / 86400.0);

    test_atomic("atomic UDP srcport 40000 (0x9c40), nxt=17", UDP_PROTO, 40000);
    test_atomic("atomic TCP srcport 40000 (0x9c40), nxt=6", TCP_PROTO, 40000);
    test_atomic("atomic UDP srcport 4353 (0x1101), accidental-match control",
                UDP_PROTO, 4353);

    test_ttl_at("ttl pre-wrap: t0=5,000,000 ms", 5000000ULL);
    test_ttl_at("ttl crossing wrap: t0=2^32-30,000 (deadline straddles wrap)",
                4294967296ULL - 30000);
    test_ttl_at("ttl at actual uptime: t0=13,900,000,000 ms (~161d, 3.24 wraps)",
                13900000000ULL);
    test_ttl_at("ttl multiple wraps: t0=10*2^32+123,456,789 (~497d)",
                10ULL * 4294967296ULL + 123456789ULL);
    test_independent_deadlines();
    test_fresh_ttl_after_reclaim();
    test_capacity_recovery();

    test_max_payload();
    test_partial_identical_overlap();
    test_conflicting_first();
    test_exact_duplicate_first();
    test_conflicting_middle();
    test_multiple_terminals();
    test_duplicate_terminal();
    test_reserved_ignored();
    test_hoplimit();
    test_output_chunking();

    printf("SUMMARY checks=%d failures=%d bug_conditions_observed=%d\n",
           g_checks, g_failures, g_bugconf);
    return g_failures ? 1 : 0;
}
