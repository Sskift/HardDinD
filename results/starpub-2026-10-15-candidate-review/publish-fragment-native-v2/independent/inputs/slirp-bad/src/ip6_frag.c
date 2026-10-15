/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * HardDinD addition on pinned libslirp 4.6.1: bounded IPv6 fragmentation
 * (RFC 8200, RFC 5722, RFC 2460). See ip6_frag.h for the security model.
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

void ip6_frag_slowtimo(Slirp *slirp, unsigned curtime_ms)
{
    struct ip6fq **pp = &slirp->ip6_reassq;
    while (*pp) {
        struct ip6fq *q = *pp;
        if (curtime_ms >= q->expire_ms) {
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

/* Tear a whole datagram down: RFC 5722 fail-closed overlap policy. */
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

/* RFC 5722 overlap comparison for an incoming fragment against a queued one.
 * Returns 1 when the intersecting window contains identical bytes, 0 on any
 * content conflict. */
static int frag_overlap_identical(const struct ip6frag *queued,
                                  const char *incoming, unsigned in_off,
                                  unsigned in_len)
{
    unsigned lo = queued->off > (int)in_off ? (unsigned)queued->off : in_off;
    unsigned qend = (unsigned)queued->off + queued->len;
    unsigned iend = in_off + in_len;
    unsigned hi = qend < iend ? qend : iend;
    if (lo >= hi) {
        return 1; /* no intersection */
    }
    const char *qp = queued->m->m_data + (lo - (unsigned)queued->off);
    const char *ip = incoming + (lo - in_off);
    return memcmp(qp, ip, hi - lo) == 0;
}

/* Build the reassembled packet (fixed IPv6 header + contiguous payload). */
static struct mbuf *frag_reassemble(Slirp *slirp, struct ip6fq *q)
{
    struct mbuf *out = m_get(slirp);
    if (!out) {
        return NULL;
    }
    m_inc(out, (int)sizeof(struct ip6) + q->end);

    struct ip6 *ip6 = mtod(out, struct ip6 *);
    memset(ip6, 0, sizeof(*ip6));
    /* Preserve version/traffic-class/flow-label raw bytes from first frag. */
    memcpy(ip6, &q->vtf, 4);
    ip6->ip_pl = htons((uint16_t)q->end);
    ip6->ip_nh = q->nxt;
    ip6->ip_hl = 64;
    ip6->ip_src = q->src;
    ip6->ip_dst = q->dst;

    char *dp = (char *)ip6 + sizeof(struct ip6);
    for (struct ip6frag *f = q->frags; f; f = f->next) {
        memcpy(dp + f->off, f->m->m_data, f->len);
    }
    out->m_len = (int)sizeof(struct ip6) + q->end;
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
    /* The 13-bit field already carries the byte offset (units of 8
     * octets, shifted to its wire position); no second *8 here. */
    unsigned byte_off = off_flags & IP6_FRAG_OFF_MASK;
    bool more = (off_flags & IP6_FRAG_M_FLAG) != 0;
    unsigned reserved = (off_flags >> 1) & 0x3;

    /* RFC 8200: both reserved fields must be zero on reception. */
    if (reserved != 0 || fh->res != 0) {
        goto drop;
    }
    unsigned chunk = m->m_len -
                    (sizeof(struct ip6) + sizeof(struct ip6_frag_hdr));
    if (byte_off == 0 && !more) {
        /* Atomic fragment: excise the 8-byte fragment header (bytes 40..47),
         * keeping the fixed header intact, and deliver immediately with its
         * next-header replaced. */
        memmove(m->m_data + sizeof(struct ip6),
                m->m_data + sizeof(struct ip6) + sizeof(struct ip6_frag_hdr),
                chunk);
        m_adj(m, -(int)sizeof(struct ip6_frag_hdr)); /* trim tail */
        ip6 = mtod(m, struct ip6 *);
        ip6->ip_nh = fh->nxt;
        ip6->ip_pl = htons((uint16_t)chunk);
        return m;
    }
    /* First fragment claiming to carry another fragment header is malformed. */
    if (byte_off == 0 && fh->nxt == IPPROTO_FRAGMENT) {
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
        q->expire_ms =
            slirp->cb->clock_get_ns(slirp->opaque) / SCALE_MS + IP6_FRAG_TTL_MS;
        q->next = slirp->ip6_reassq;
        slirp->ip6_reassq = q;
    }

    if (byte_off == 0) {
        if (q->have_first) {
            goto drop; /* two conflicting first fragments */
        }
        q->have_first = 1;
        q->nxt = fh->nxt;
        memcpy(&q->vtf, ip6, 4);
    }
    if (slirp->ip6_frag_count >= IP6_FRAG_MAX_FRAGMENTS) {
        goto drop;
    }

    /* Remove both headers, keeping ownership of the payload mbuf locally. */
    m_adj(m, (int)(sizeof(struct ip6) + sizeof(struct ip6_frag_hdr)));
    struct mbuf *payload = m;
    const char *pdata = payload->m_data;

    /* Overlap policy (RFC 5722): identical duplicate coverage is ignored,
     * any conflicting overlap destroys the whole datagram. */
    for (struct ip6frag *g = q->frags; g; g = g->next) {
        if (!frag_overlap_identical(g, pdata, byte_off, chunk)) {
            frag_discard_queue(slirp, q);
            m_free(payload);
            return NULL;
        }
    }

    /* Exact duplicate (same offset, same length, identical bytes): ignore. */
    for (struct ip6frag *g = q->frags; g; g = g->next) {
        if ((unsigned)g->off == byte_off && (unsigned)g->len == chunk) {
            m_free(payload);
            return NULL;
        }
    }

    struct ip6frag *nf = g_new(struct ip6frag, 1);
    nf->m = payload;
    nf->off = (int)byte_off;
    nf->len = (int)chunk;

    /* Insert ordered by offset. */
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

    /* Completion needs the first fragment (for nxt), the final one (for the
     * total length) and gap-free coverage [0,end). */
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
    /* Unlink (frag_reassemble copied the data) and free everything. */
    struct ip6fq **link = &slirp->ip6_reassq;
    while (*link != q) {
        link = &(*link)->next;
    }
    *link = q->next;
    frag_free_queue(slirp, q);
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
    /* Fragmentable payload per fragment (8-byte aligned). */
    int chunk_max = (slirp->if_mtu - (int)sizeof(struct ip6) -
                     (int)sizeof(struct ip6_frag_hdr)) &
                    ~7;
    if (chunk_max < 8 || plen <= 0) {
        m_free(m);
        return -1;
    }
    /* Never refragment an already fragmented packet (RFC: fragments are
     * produced by the source node; slirp never emits nested fragments). */
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
        fh->res = 0;
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
    /* Best-effort cleanup of fragments already queued by if_output is not
     * possible (they are independent packets); free the remainder and the
     * original. Callers treat this as a dropped datagram. */
    (void)emitted;
    m_free(m);
    return -1;
}
