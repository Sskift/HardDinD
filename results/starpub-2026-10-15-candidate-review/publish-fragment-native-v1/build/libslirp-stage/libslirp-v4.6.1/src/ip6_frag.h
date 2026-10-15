/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * HardDinD addition on pinned libslirp 4.6.1: bounded IPv6 fragmentation
 * support (RFC 8200), both output fragmentation and input reassembly.
 * Upstream libslirp through 4.9.5 has no IPv6 fragmentation at all.
 *
 * Security model:
 *  - fixed-size queue/entry table (no unbounded allocation per peer)
 *  - every fragment is bounds-checked against RFC 8200 limits
 *  - reassembled fragmentable payload never exceeds 65535 bytes (the IPv6
 *    payload length field excludes the 40-byte fixed header); the complete
 *    packet may therefore be up to 40+65535 bytes
 *  - exact duplicate fragments (same offset/length/M/data) are dropped while
 *    the reassembly queue is retained; ANY non-exact overlap discards the
 *    whole datagram queue (RFC 8200 section 4.5 fail-closed policy)
 *  - every incomplete datagram is reaped after 60 s using the same 32-bit
 *    millisecond clock slirp already uses (wrap-safe signed comparison, so
 *    expiry works past 2^32 ms uptime)
 *  - the 8-bit reserved byte and the two reserved flag bits are ignored on
 *    reception as required by RFC 8200; generated fragments send them zero
 *  - jumbograms (hop-by-hop payload length extension) are not implemented
 */
#ifndef SLIRP_IP6_FRAG_H
#define SLIRP_IP6_FRAG_H

#include <stdint.h>

/* RFC 2460/8200 reassembly timeout (60 s), in slirp's 32-bit ms clock domain. */
#define IP6_FRAG_TTL_MS 60000u
/* Maximum number of concurrently reassembled datagrams. */
#define IP6_FRAG_MAX_QUEUES 64u
/* Maximum number of fragment mbufs held across all queues. A maximum-size
 * datagram fragmented at the smallest 8-byte granularity needs 8192 pieces,
 * so the bound is exactly that: bounded (~12 MiB of 1500-byte mbufs) but
 * sufficient for any legal IPv6 packet. */
#define IP6_FRAG_MAX_FRAGMENTS 8192u
/* Maximum fragmentable payload (IPv6 payload length excludes the fixed
 * header, so this is 65535, not 65535-40). Total packet can reach 65575. */
#define IP6_FRAG_MAX_PAYLOAD 65535u

/* One received fragment; its mbuf holds the fragmentable payload only
 * (both the fixed IPv6 header and the fragment header have been removed). */
struct ip6frag {
    struct ip6frag *next;
    struct mbuf *m;
    int off; /* byte offset within the fragmentable part */
    int len; /* payload bytes */
};

/* Reassembly state for one original datagram, keyed by the RFC 8200 tuple. */
struct ip6fq {
    struct ip6fq *next;
    struct in6_addr src;
    struct in6_addr dst;
    uint32_t id;
    uint32_t vtf;         /* version/traffic-class/flow-label word from frag 0 */
    uint32_t expire_ms;   /* deadline in slirp's wrapping 32-bit ms clock */
    struct ip6frag *frags; /* ordered by offset */
    int nfrags;
    int end;              /* total fragmentable length once the last frag is in */
    uint8_t nxt;          /* next header from the first fragment */
    uint8_t hop_limit;    /* hop limit from the first fragment, preserved */
    uint8_t have_first;
};

/* Set up / tear down reassembly state. */
void ip6_frag_init(Slirp *slirp);
void ip6_frag_cleanup(Slirp *slirp);
/* Reap expired queues; invoked from the 500 ms slow timer with slirp's
 * truncated 32-bit monotonic millisecond clock. Comparison is wrap-safe. */
void ip6_frag_slowtimo(Slirp *slirp, uint32_t curtime_ms);

/* Handle a packet whose fixed header next-header is IPPROTO_FRAGMENT.
 * Returns a complete reassembled mbuf (fixed header + payload, fixed header
 * next-header replaced by the fragment header's value) or NULL if the packet
 * was consumed, malformed or is still waiting for more fragments. The caller
 * must not touch m after a non-NULL return (it has been consumed either way). */
struct mbuf *ip6_frag_input(Slirp *slirp, struct mbuf *m);

/* Split a unicast IPv6 packet larger than the interface MTU into properly
 * formed fragments queued with if_output(). At MTU 1500 the fragmentable
 * chunk is (1500-40-8)&~7 = 1448 bytes (frame sizes 40+8+1448=1496 IP,
 * 1510 Ethernet). Consumes m (frees on success and on failure). Returns 0 on
 * success, -1 if the packet could not be emitted (caller still must not touch
 * m). */
int ip6_frag_output(Slirp *slirp, struct socket *so, struct mbuf *m);

#endif
