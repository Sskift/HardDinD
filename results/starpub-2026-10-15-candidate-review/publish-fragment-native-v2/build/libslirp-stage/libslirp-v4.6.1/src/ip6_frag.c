/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * HardDinD addition on pinned libslirp 4.6.1: bounded IPv6 fragmentation
 * (RFC 8200). See ip6_frag.h for the security model.
 */
#include "slirp.h"
#include "ip6_frag.h"

/* Fragment header (RFC 8200 section 4.5), 8 bytes. */
struct ip6_frag_hdr {
    uint8_t nxt;
    uint8_t res;
    uint16_t off_flags; /* high 13 bits: offset/8; bit 0: M; bits 1-2: reserved */
    uint32_t id;
};

#define IP6_FRAG_M_FLAG 0x0001
#define IP6_FRAG_OFF_SHIFT 3
#define IP6_FRAG_OFF_MASK 0xfff8

void ip6_frag_init(Slirp *slirp)
{
    slirp->ip6_reassq = NULL;
    /* Identification needs no cryptographic quality: the RFC 8200 tuple
     * (src,dst,id) disambiguates datagrams. */
    slirp->ip6_frag_id = 0x1000;
    slirp->ip6_frag_count = 0;
}

static void frag_free_frag(Slirp *slirp, struct ip6frag *f)
{
    slirp->ip6_frag_count--;
    m_free(f->m);
    g_free(f);
}

static void frag_free_queue(Slirp *slirp, struct ip6fq *q)
{
    struct ip6frag *f = q->frags;
    while (f) {
        struct ip6frag *next = f->next;
        frag_free_frag(slirp, f);
        f = next;
    }
    g_free(q);
}

void ip6_frag_cleanup(Slirp *slirp)
{
    struct ip6fq *q = slirp->ip6_reassq;
    while (q) {
        struct ip6fq *next = q->next;
        frag_free_queue(slirp, q);
        q = next;
    }
    slirp->ip6_reassq = NULL;
}

void ip6_frag_slowtimo(Slirp *slirp, uint32_t curtime_ms)
{
    struct ip6fq **pp = &slirp->ip6_reassq;
    while (*pp) {
        struct ip6fq *q = *pp;
        /* Same wrapping 32-bit millisecond clock that set expire_ms: signed
         * subtraction stays correct across the 2^32 rollover for a 60 s TTL. */
        int32_t remaining = (int32_t)(q->expire_ms - curtime_ms);
        if (remaining <= 0) {
            *pp = q->next;
            frag_free_queue(slirp, q);
            continue;
        }
        pp = &q->next;
    }
}

static struct ip6fq *frag_find(Slirp *slirp, const struct in6_addr *src,
                               const struct in6_addr *dst, uint32_t id)
{
    for (struct ip6fq *q = slirp->ip6_reassq; q; q = q->next) {
        if (q->id == id && in6_equal(&q->src, src) && in6_equal(&q->dst, dst)) {
            return q;
        }
    }
    return NULL;
}

static unsigned frag_queue_count(Slirp *slirp)
{
    unsigned n = 0;
    for (struct ip6fq *q = slirp->ip6_reassq; q; q = q->next) {
        n++;
    }
    return n;
}

/* Unlink and free an existing queue. Used when RFC 8200 overlap rules require
 * discarding the whole reassembly state. */
static void frag_discard_queue(Slirp *slirp, struct ip6fq *victim)
{
    struct ip6fq **pp = &slirp->ip6_reassq;
    while (*pp && *pp != victim) {
        pp = &(*pp)->next;
    }
    if (*pp) {
        *pp = victim->next;
        frag_free_queue(slirp, victim);
    }
}

/* Intersection window of a queued fragment [qoff,qoff+qlen) with an incoming
 * fragment [ioff,ioff+ilen). Returns 1 when they overlap. */
static int frag_window(const struct ip6frag *g, unsigned ioff, unsigned ilen,
                       unsigned *lo, unsigned *hi)
{
    unsigned qend = (unsigned)g->off + g->len;
    unsigned iend = ioff + ilen;
    *lo = (unsigned)g->off > ioff ? (unsigned)g->off : ioff;
    *hi = qend < iend ? qend : iend;
    return *lo < *hi;
}

/* Whether an incoming fragment is byte-identical to a queued one across their
 * intersecting window. */
static int frag_window_identical(const struct ip6frag *g, const char *incoming,
                                 unsigned ioff, unsigned lo, unsigned hi)
{
    const char *qp = g->m->m_data + (lo - (unsigned)g->off);
    const char *ip = incoming + (lo - ioff);
    return memcmp(qp, ip, hi - lo) == 0;
}

/* Build the reassembled packet (fixed IPv6 header + contiguous payload). */
static struct mbuf *frag_reassemble(Slirp *slirp, struct ip6fq *q)
{
    struct mbuf *out = m_get(slirp);
    if (!out) {
        return NULL;
    }
    /* Reproduce the exact mbuf positioning of the Ethernet ingress path
     * (slirp_input): the fixed IPv6 header sits TCPIPHDR_DELTA+2+ETH_HLEN
     * bytes after the mbuf's start so tcp_input's backwards pull-up of the
     * pseudo-header/qlink stays naturally 8-byte aligned. Building the
     * packet with m_data at m_dat would trip alignment faults and misparse
     * TCP. */
    /* Must match the v6 ingress placement in slirp_input: DELTA + 6 +
     * ETH_HLEN = 48 bytes, giving 8-byte qlink alignment in tcp_input. */
    unsigned head = TCPIPHDR_DELTA + 6 + ETH_HLEN;
    m_inc(out, (int)(head + sizeof(struct ip6) + q->end));
    out->m_data += head;
    out->m_len = (int)(sizeof(struct ip6) + q->end);

    struct ip6 *ip6 = mtod(out, struct ip6 *);
    memset(ip6, 0, sizeof(*ip6));
    /* Preserve version/traffic-class/flow-label and hop limit from the first
     * fragment rather than inventing values. */
    memcpy(ip6, &q->vtf, 4);
    ip6->ip_pl = htons((uint16_t)q->end);
    ip6->ip_nh = q->nxt;
    ip6->ip_hl = q->hop_limit;
    ip6->ip_src = q->src;
    ip6->ip_dst = q->dst;

    char *dp = (char *)ip6 + sizeof(struct ip6);
    for (struct ip6frag *f = q->frags; f; f = f->next) {
        memcpy(dp + f->off, f->m->m_data, f->len);
    }
    return out;
}

struct mbuf *ip6_frag_input(Slirp *slirp, struct mbuf *m)
{
    struct ip6 *ip6 = mtod(m, struct ip6 *);

    if (m->m_len < (int)(sizeof(struct ip6) + sizeof(struct ip6_frag_hdr))) {
        goto drop;
    }
    const struct ip6_frag_hdr *fh =
        (const struct ip6_frag_hdr *)(m->m_data + sizeof(struct ip6));
    uint16_t off_flags = ntohs(fh->off_flags);
    uint32_t id = ntohl(fh->id);
    /* The 13-bit field carries the offset in units of 8 octets; it is already
     * the byte offset (shifted into its wire position), so no second *8. */
    unsigned byte_off = off_flags & IP6_FRAG_OFF_MASK;
    bool more = (off_flags & IP6_FRAG_M_FLAG) != 0;
    /* Capture every field from the fragment header BEFORE editing the packet
     * (the atomic path below memmoves over fh's bytes). */
    uint8_t nxt = fh->nxt;
    /* Reserved byte/flag bits are ignored on reception (RFC 8200 4.5). */
    unsigned chunk = m->m_len -
                    (sizeof(struct ip6) + sizeof(struct ip6_frag_hdr));
    uint8_t hop_limit = ip6->ip_hl;

    if (byte_off == 0 && !more) {
        /* Atomic fragment: excise the 8-byte fragment header (bytes 40..47),
         * keeping the fixed header intact, and deliver immediately. nxt was
         * captured above; reading fh->nxt after the memmove would return the
         * first payload byte. */
        memmove(m->m_data + sizeof(struct ip6),
                m->m_data + sizeof(struct ip6) + sizeof(struct ip6_frag_hdr),
                chunk);
        m_adj(m, -(int)sizeof(struct ip6_frag_hdr)); /* trim tail */
        ip6 = mtod(m, struct ip6 *);
        ip6->ip_nh = nxt;
        ip6->ip_pl = htons((uint16_t)chunk);
        ip6->ip_hl = hop_limit;
        return m;
    }
    /* First fragment claiming to carry another fragment header is malformed. */
    if (byte_off == 0 && nxt == IPPROTO_FRAGMENT) {
        goto drop;
    }

    if (more && (chunk % 8) != 0) {
        goto drop; /* non-final fragments are 8-octet aligned */
    }
    if (chunk == 0) {
        goto drop;
    }
    unsigned end = byte_off + chunk;
    if (end > IP6_FRAG_MAX_PAYLOAD) {
        goto drop;
    }

    bool new_queue = false;
    struct ip6fq *q = frag_find(slirp, &ip6->ip_src, &ip6->ip_dst, id);
    if (!q) {
        if (frag_queue_count(slirp) >= IP6_FRAG_MAX_QUEUES) {
            goto drop;
        }
        q = g_new0(struct ip6fq, 1);
        q->src = ip6->ip_src;
        q->dst = ip6->ip_dst;
        q->id = id;
        q->nxt = 0xff;
        q->end = -1;
        /* Deadline in slirp's wrapping 32-bit ms clock domain. */
        uint32_t now =
            (uint32_t)(slirp->cb->clock_get_ns(slirp->opaque) / SCALE_MS);
        q->expire_ms = now + IP6_FRAG_TTL_MS;
        q->next = slirp->ip6_reassq;
        slirp->ip6_reassq = q;
        new_queue = true;
    }

    const char *incoming = m->m_data + sizeof(struct ip6) +
                           sizeof(struct ip6_frag_hdr);

    /* ---- Validate against existing state BEFORE mutating the queue. ---- */

    /* Two terminal fragments must agree on the total length. A conflicting
     * terminal discards the whole existing reassembly (and a queue we just
     * tentatively created never reaches this point with end set). */
    if (!more && q->end >= 0 && (unsigned)q->end != end) {
        frag_discard_queue(slirp, q);
        goto drop;
    }

    /* Exact duplicate: a queued fragment with the same offset, length, M
     * semantics and identical bytes. RFC 8200: drop the copy but retain the
     * queue. A same-offset fragment that differs in any way is NOT a
     * duplicate and triggers full discard below. */
    bool duplicate = false;
    for (struct ip6frag *g = q->frags; g; g = g->next) {
        if ((unsigned)g->off != byte_off) {
            continue;
        }
        bool queued_terminal =
            (q->end >= 0 && (unsigned)(g->off + g->len) == (unsigned)q->end);
        bool m_agrees = (more == !queued_terminal);
        if ((unsigned)g->len == chunk && m_agrees &&
            memcmp(g->m->m_data, incoming, chunk) == 0) {
            duplicate = true;
            break;
        }
        /* Same offset, different content/length/M: non-exact overlap. */
        frag_discard_queue(slirp, q);
        goto drop;
    }
    if (duplicate) {
        if (new_queue) {
            frag_free_queue(slirp, q);
        }
        goto drop; /* incoming copy freed below; retained queue untouched */
    }

    /* Any partial intersection with a different-offset queued fragment is a
     * non-exact overlap even if the intersecting bytes happen to match:
     * discard the whole datagram (RFC 8200 4.5). */
    for (struct ip6frag *g = q->frags; g; g = g->next) {
        unsigned lo, hi;
        if (frag_window(g, byte_off, chunk, &lo, &hi) &&
            !frag_window_identical(g, incoming, byte_off, lo, hi)) {
            frag_discard_queue(slirp, q);
            goto drop;
        }
    }
    /* A partial intersection where the bytes do match but the fragments do
     * not exactly coincide must also discard the queue (only exact
     * duplicates are tolerated). */
    for (struct ip6frag *g = q->frags; g; g = g->next) {
        unsigned lo, hi;
        if (frag_window(g, byte_off, chunk, &lo, &hi) &&
            !((unsigned)g->off == byte_off && (unsigned)g->len == chunk)) {
            frag_discard_queue(slirp, q);
            goto drop;
        }
    }
    /* A non-terminal fragment must not extend past an established total. */
    if (more && q->end >= 0 && end > (unsigned)q->end) {
        frag_discard_queue(slirp, q);
        goto drop;
    }

    /* ---- Commit: headers removed, fragment inserted, first fields set. ---- */
    m_adj(m, (int)(sizeof(struct ip6) + sizeof(struct ip6_frag_hdr)));
    struct mbuf *payload = m;

    if (byte_off == 0) {
        /* Duplicate first fragments were handled above; reaching here with an
         * existing first fragment means it was an exact same-offset copy,
         * already dropped - so this is the first first. */
        q->have_first = 1;
        q->nxt = nxt;
        memcpy(&q->vtf, ip6, 4);
        q->hop_limit = hop_limit;
    }
    if (slirp->ip6_frag_count >= IP6_FRAG_MAX_FRAGMENTS) {
        m_free(payload);
        return NULL;
    }

    struct ip6frag *nf = g_new(struct ip6frag, 1);
    nf->m = payload;
    nf->off = (int)byte_off;
    nf->len = (int)chunk;

    struct ip6frag **pp = &q->frags;
    while (*pp && (*pp)->off < nf->off) {
        pp = &(*pp)->next;
    }
    nf->next = *pp;
    *pp = nf;
    q->nfrags++;
    slirp->ip6_frag_count++;

    if (!more) {
        q->end = (int)end;
    }

    /* Completion requires the first fragment (nxt/hop), a final fragment
     * (total length) and gap-free coverage [0,end). */
    if (!q->have_first || q->end < 0) {
        return NULL;
    }
    int expect = 0;
    for (struct ip6frag *f = q->frags; f; f = f->next) {
        if (f->off != expect) {
            return NULL;
        }
        expect = f->off + f->len;
    }
    if (expect != q->end) {
        return NULL;
    }

    struct mbuf *out = frag_reassemble(slirp, q);
    frag_discard_queue(slirp, q); /* unlink and free; data was copied into out */
    return out;

drop:
    m_free(m);
    return NULL;
}

int ip6_frag_output(Slirp *slirp, struct socket *so, struct mbuf *m)
{
    struct ip6 *ip6 = mtod(m, struct ip6 *);
    int total = m->m_len;
    int plen = total - (int)sizeof(struct ip6);

    if (total <= slirp->if_mtu) {
        if_output(so, m);
        return 0;
    }
    /* Fragmentable payload per fragment (8-byte aligned): at if_mtu 1500 this
     * is (1500-40-8)&~7 = 1448 bytes, i.e. 496-byte IP / 1510-byte Ethernet
     * frames with the trailing fragment carrying the remainder. */
    int chunk_max = (slirp->if_mtu - (int)sizeof(struct ip6) -
                     (int)sizeof(struct ip6_frag_hdr)) &
                    ~7;
    if (chunk_max < 8 || plen <= 0) {
        m_free(m);
        return -1;
    }
    /* Never refragment an already fragmented packet (fragments are produced
     * by the source node; slirp never emits nested fragments). */
    if (ip6->ip_nh == IPPROTO_FRAGMENT) {
        if_output(so, m);
        return 0;
    }

    uint8_t orig_nh = ip6->ip_nh;
    uint32_t id = htonl(slirp->ip6_frag_id++);
    int off = 0;
    int emitted = 0;

    while (off < plen) {
        int chunk = plen - off;
        if (chunk > chunk_max) {
            chunk = chunk_max;
        }
        bool last = (off + chunk == plen);

        struct mbuf *f = m_get(slirp);
        if (!f) {
            goto fail;
        }
        m_inc(f, (int)sizeof(struct ip6) + (int)sizeof(struct ip6_frag_hdr) + chunk);

        char *fp = f->m_data;
        /* Fixed header, with next-header replaced by IPPROTO_FRAGMENT. */
        memcpy(fp, ip6, sizeof(struct ip6));
        struct ip6 *fh6 = (struct ip6 *)fp;
        fh6->ip_nh = IPPROTO_FRAGMENT;
        fh6->ip_pl = htons((uint16_t)(sizeof(struct ip6_frag_hdr) + chunk));

        struct ip6_frag_hdr *fh =
            (struct ip6_frag_hdr *)(fp + sizeof(struct ip6));
        fh->nxt = orig_nh;
        fh->res = 0; /* generated fragments always zero the reserved byte */
        uint16_t off_flags = (uint16_t)(((off / 8) << IP6_FRAG_OFF_SHIFT) &
                                        IP6_FRAG_OFF_MASK);
        if (!last) {
            off_flags |= IP6_FRAG_M_FLAG;
        }
        fh->off_flags = htons(off_flags);
        memcpy(&fh->id, &id, sizeof(id));

        memcpy(fp + sizeof(struct ip6) + sizeof(struct ip6_frag_hdr),
               m->m_data + sizeof(struct ip6) + off, chunk);
        f->m_len = (int)sizeof(struct ip6) + (int)sizeof(struct ip6_frag_hdr) +
                   chunk;

        if_output(so, f);
        emitted++;
        off += chunk;
    }

    m_free(m);
    return 0;

fail:
    /* Fragments already queued by if_output are independent packets and
     * cannot be retracted; free the remainder and the original. Callers treat
     * this as a dropped datagram. */
    (void)emitted;
    m_free(m);
    return -1;
}
