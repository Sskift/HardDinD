/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * HardDinD addition on pinned libslirp 4.6.1: bounded IPv6 fragmentation
 * support (RFC 8200 / RFC 5722 / RFC 2460), both output fragmentation and
 * input reassembly. Upstream libslirp through 4.9.5 has no IPv6 fragmentation
 * at all; large UDP replies from an IPv6-capable guest were silently dropped
 * because ip6_input() freed next-header 44 (IPPROTO_FRAGMENT) frames.
 *
 * Security model:
 *  - fixed-size queue/entry table (no unbounded allocation per peer)
 *  - every fragment is bounds-checked against RFC 8200 limits
 *  - reassembled payloads never exceed 65535 - sizeof(ip6)
 *  - overlapping fragments with conflicting data tear the whole datagram
 *    down (RFC 5722 fail-closed policy); byte-identical duplicates are
 *    accepted and ignored
 *  - every incomplete datagram is reaped on a 60 s timer
 */
#ifndef SLIRP_IP6_FRAG_H
#define SLIRP_IP6_FRAG_H

#include <stdint.h>

/* RFC 2460 reassembly timeout (60 s). */
#define IP6_FRAG_TTL_MS 60000u
/* Maximum number of concurrently reassembled datagrams. */
#define IP6_FRAG_MAX_QUEUES 64u
/* Maximum number of fragment mbufs held across all queues. */
#define IP6_FRAG_MAX_FRAGMENTS 1024u
/* Largest original packet (fixed header + fragmentable part), RFC 8200. */
#define IP6_FRAG_MAX_PACKET 65535u
#define IP6_FRAG_MAX_PAYLOAD (IP6_FRAG_MAX_PACKET - (unsigned)sizeof(struct ip6))

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
    uint32_t vtf;       /* version/traffic-class/flow-label word from frag 0 */
    uint64_t expire_ms; /* curtime deadline */
    struct ip6frag *frags; /* ordered by offset */
    int nfrags;
    int end;           /* total fragmentable length once the last frag is in */
    uint8_t nxt;       /* next header from the first fragment */
    uint8_t have_first;
};

/* Set up / tear down reassembly state. */
void ip6_frag_init(Slirp *slirp);
void ip6_frag_cleanup(Slirp *slirp);
/* Reap expired queues; invoked from the 500 ms slow timer. */
void ip6_frag_slowtimo(Slirp *slirp, unsigned curtime_ms);

/* Handle a packet whose fixed header next-header is IPPROTO_FRAGMENT.
 * Returns a complete reassembled mbuf (fixed header + payload, fixed header
 * next-header replaced by the fragment header's value) or NULL if the packet
 * was consumed, malformed or is still waiting for more fragments. The caller
 * must not touch m after a non-NULL return (it has been consumed either way). */
struct mbuf *ip6_frag_input(Slirp *slirp, struct mbuf *m);

/* Split a unicast IPv6 packet larger than the interface MTU into properly
 * formed fragments queued with if_output(). Consumes m (frees it on success
 * and on failure). Returns 0 on success, -1 if the packet could not be
 * emitted (caller still must not touch m). */
int ip6_frag_output(Slirp *slirp, struct socket *so, struct mbuf *m);

#endif
