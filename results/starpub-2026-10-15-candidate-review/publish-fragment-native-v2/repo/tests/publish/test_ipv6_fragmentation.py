"""IPv6 fragmentation/reassembly against the real patched libslirp backend.

Two distinct directions are exercised over an IPv6 hostfwd on ::1:

A. full-size data path (the owner's failing scenario): outer IPv6 UDP at
   1/1200/1400/1452/1453/2000/8000/60000 must echo. Slirp must split to_guest
   datagrams above 1452 into fragments (verified on the wire) and reassemble
   the model guest's fragmented replies.

B. raw reassembly hardening: crafted guest->slirp fragment sets cover
   out-of-order delivery, duplicates, RFC 5722 overlap conflicts, malformed
   headers/offsets, bounded waits for missing tails, independent flow IDs and
   truncated fragment headers. Malformed input must be dropped silently with
   no crash (also run under ASan/UBSan).
"""
import os
import socket
import struct
import sys
import time
import unittest

from backend import Backend, reserve_port
from slirp_peer import SlirpPeer, IP6, l4_checksum, ETH_P_IPV6

VHOST6 = bytes.fromhex('fd00' + '0000' * 6 + '0002')
SIZES = [1, 1200, 1400, 1452, 1453, 2000, 8000, 60000]


def _v6_recv(sock, timeout=5.0):
    sock.settimeout(timeout)
    try:
        return sock.recv(65536)
    except socket.timeout:
        return None


def _pattern(n, seed=0):
    return bytes((i * 7 + seed) & 0xff for i in range(n))


class FragmentedFullPath(unittest.TestCase):
    def setUp(self):
        self.b = Backend().start()
        self.addCleanup(self.b.stop)
        self.peer = SlirpPeer(self.b.link_host, udp_ports=(7777,),
                              v6_udp_reply_mtu=1500)
        self.peer.start()
        self.addCleanup(self.peer.stop)
        self.port = reserve_port('::1', udp=True)
        self.b.ok(f'create udp ::1 {self.port} 7777 fd00::15')
        self.addCleanup(self.b.command, f'del udp ::1 {self.port} 7777 fd00::15')

    def test_udp_all_sizes_and_wire_fragments(self):
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.connect(('::1', self.port))
        for size in SIZES:
            self.peer.inbound_v6_fragments.clear()
            body = _pattern(size, seed=size & 0xff)
            s.send(body)
            got = _v6_recv(s)
            self.assertIsNotNone(got, f'size {size} timed out')
            self.assertEqual(got, body, f'size {size} payload mismatch ({len(got)})')
            if size > 1452:
                frags = self.peer.inbound_v6_fragments
                self.assertTrue(frags, f'size {size}: slirp sent no fragments to guest')
                # Every link frame <= 1500 (40 ip6 + 8 frag + 1452 payload).
                for f in frags:
                    self.assertLessEqual(len(f), 1500)
                    self.assertEqual(f[6], 44, 'next-header must be FRAGMENT')
                # First fragment carries nh=UDP (17); offsets are ascending.
                first_flags = struct.unpack('!H', frags[0][42:44])[0]
                self.assertEqual(frags[0][40], 17)
                self.assertEqual(first_flags >> 3, 0)
                offsets = [(struct.unpack("!H", f[42:44])[0] >> 3) * 8 for f in frags]
                self.assertEqual(offsets, sorted(offsets))
                # Last fragment clears M; all earlier ones set it.
                last_flags = struct.unpack('!H', frags[-1][42:44])[0]
                self.assertEqual(last_flags & 1, 0)
                self.assertTrue(all(struct.unpack("!H", f[42:44])[0] & 1 for f in frags[:-1]))
                # Reassembled total covers the whole datagram.
                total = sum(len(f) - 48 for f in frags)
                self.assertEqual(total, 8 + size)


class RawReassembly(unittest.TestCase):
    def setUp(self):
        self.b = Backend().start()
        self.addCleanup(self.b.stop)
        # Model guest does not auto-echo v6 UDP; tests inject fragments.
        self.peer = SlirpPeer(self.b.link_host, udp_ports=(7777,), raw_v6_udp=True)
        self.peer.start()
        self.addCleanup(self.peer.stop)
        self.port = reserve_port('::1', udp=True)
        self.b.ok(f'create udp ::1 {self.port} 7777 fd00::15')
        self.addCleanup(self.b.command, f'del udp ::1 {self.port} 7777 fd00::15')
        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.addCleanup(self.sock.close)
        self.sock.connect(('::1', self.port))
        self._ident = 0

    def _prime_and_sport(self):
        """Send one datagram so slirp resolves NDP and creates the udb session;
        return slirp's guest-side UDP source port."""
        self.peer.inbound_v6_udp_ports.clear()
        self.sock.send(b'prime')
        deadline = time.time() + 3
        while time.time() < deadline:
            if self.peer.inbound_v6_udp_ports:
                break
            time.sleep(0.02)
        self.assertTrue(self.peer.inbound_v6_udp_ports, 'no slirp->guest datagram')
        sport, dport = self.peer.inbound_v6_udp_ports[0]
        self.assertEqual(dport, 7777)
        return sport

    def _udp_packet(self, sport, body, n=1200):
        out = struct.pack('!HHHH', 7777, sport, 8 + len(body), 0) + body
        check = l4_checksum(IP6, VHOST6, 17, out)
        return out[:6] + struct.pack('!H', check) + out[8:]

    def _inject(self, frames):
        for f in frames:
            self.peer.inject_eth(f)

    def _next_id(self):
        self._ident += 1
        return 0x6000 + self._ident

    def _clean_datagram(self, sport, n, ident=None, order=None):
        body = _pattern(n, seed=(ident or self._next_id()) & 0xff)
        pkt = self._udp_packet(sport, body, n)
        ident = ident or self._next_id()
        frames = self.peer.build_v6_fragments(IP6, VHOST6, 17, pkt, ident, 1452)
        if order == 'reverse':
            frames = list(reversed(frames))
        elif order == 'shuffle':
            mid = frames[1:2]
            frames = [frames[0]] + list(reversed(frames[1:])) if mid else frames
        return body, frames

    def test_out_of_order_fragments_deliver(self):
        sport = self._prime_and_sport()
        body, frames = self._clean_datagram(sport, 2000, order='reverse')
        self._inject(frames)
        self.assertEqual(_v6_recv(self.sock, 2.0), body)

    def test_identical_duplicates_deliver_once(self):
        sport = self._prime_and_sport()
        body, frames = self._clean_datagram(sport, 2000)
        # Duplicate each fragment while the queue is still incomplete (the
        # tail is sent last); RFC 5722 identical coverage is ignored.
        interleaved = []
        for f in frames[:-1]:
            interleaved += [f, f]
        interleaved.append(frames[-1])
        self._inject(interleaved)
        got = _v6_recv(self.sock, 2.0)
        self.assertEqual(got, body)
        # No second datagram should appear.
        self.sock.settimeout(0.4)
        try:
            extra = self.sock.recv(65536)
            self.fail(f'unexpected duplicate delivery: {len(extra)} bytes')
        except socket.timeout:
            pass

    def test_conflicting_overlap_drops_datagram(self):
        sport = self._prime_and_sport()
        body, frames = self._clean_datagram(sport, 2000, ident=0x7001)
        # Corrupt overlapping bytes in the second fragment, keep first valid.
        evil = bytearray(frames[1])
        evil[-1] ^= 0xff
        self._inject([frames[0], bytes(evil)])
        self.assertIsNone(_v6_recv(self.sock, 0.6))
        # The torn-down datagram must not poison subsequent traffic: a clean
        # new flow reassembles and delivers.
        body2, frames2 = self._clean_datagram(sport, 2000, ident=0x7002)
        self.sock.send(b'prime2')
        time.sleep(0.1)
        self._inject(frames2)
        self.assertEqual(_v6_recv(self.sock, 2.0), body2)

    def test_atomic_udp_fragment_with_non17_source_port(self):
        """Real-library counterexample for the atomic-fragment fh->nxt
        overwrite bug: the inner UDP source port is 7777 (0x1E61, high byte
        0x1E != 0x11), so a stale fh->nxt read after the header-excising
        memmove would dispatch to a non-UDP protocol and no datagram arrives.
        """
        from slirp_peer import SlirpPeer as P
        b = Backend().start()
        self.addCleanup(b.stop)
        peer = P(b.link_host, udp_ports=(7777,), atomic_udp=True)
        peer.start()
        self.addCleanup(peer.stop)
        port = reserve_port('::1', udp=True)
        b.ok(f'create udp ::1 {port} 7777 fd00::15')
        self.addCleanup(b.command, f'del udp ::1 {port} 7777 fd00::15')
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.connect(('::1', port))
        for size in (1, 100, 1444):
            s.send(_pattern(size, seed=size))
            got = _v6_recv(s, 2.0)
            self.assertIsNotNone(got, f'atomic UDP echo size {size}')
            self.assertEqual(got, _pattern(size, seed=size))

    def test_atomic_tcp_fragment_handshake(self):
        """The guest SYNACK and data arrive wrapped in atomic fragment headers
        (nxt=6). Without the header-excise fix the TCP engine never sees them
        and connect() times out."""
        from slirp_peer import SlirpPeer as P
        b = Backend().start()
        self.addCleanup(b.stop)
        peer = P(b.link_host, atomic_tcp=True)
        peer.start()
        self.addCleanup(peer.stop)
        port = reserve_port('::1')
        b.ok(f'create tcp ::1 {port} 7777 fd00::15')
        self.addCleanup(b.command, f'del tcp ::1 {port} 7777 fd00::15')
        s = socket.create_connection(('::1', port), timeout=3)
        self.addCleanup(s.close)
        s.settimeout(3)
        payload = b'atomic-tcp-ok'
        s.sendall(payload)
        chunks = []
        while sum(map(len, chunks)) < len(payload):
            chunks.append(s.recv(64))
        self.assertEqual(b''.join(chunks), payload)

    def test_identical_partial_overlap_discards_queue(self):
        sport = self._prime_and_sport()
        body = _pattern(1452 + 100)
        pkt = self._udp_packet(sport, body)
        # [0,1452) then [8, 1460): overlapping bytes match but it is not an
        # exact duplicate -> whole queue must be discarded.
        frames = self.peer.build_v6_fragments(
            IP6, VHOST6, 17, pkt, 0x7040, 1452, malformed='identicalpartial')
        self._inject(frames)
        self.assertIsNone(_v6_recv(self.sock, 0.6))
        # The flow is not poisoned: a clean datagram delivers afterwards.
        clean_body, clean = self._clean_datagram(sport, 1452, ident=0x7041)
        self.sock.send(b'after-discard')
        time.sleep(0.1)
        self._inject(clean)
        self.assertEqual(_v6_recv(self.sock, 2.0), clean_body)

    def test_conflicting_first_fragment_discards_queue(self):
        sport = self._prime_and_sport()
        body = _pattern(2000)
        pkt = self._udp_packet(sport, body)
        frames = self.peer.build_v6_fragments(
            IP6, VHOST6, 17, pkt, 0x7050, 1452, malformed='conflictingfirst')
        self._inject(frames)
        self.assertIsNone(_v6_recv(self.sock, 0.6))
        clean_body, clean = self._clean_datagram(sport, 1452, ident=0x7051)
        self._inject(clean)
        self.assertEqual(_v6_recv(self.sock, 2.0), clean_body)

    def test_nonfinal_unaligned_chunk_dropped(self):
        sport = self._prime_and_sport()
        body = _pattern(2000)
        pkt = self._udp_packet(sport, body)
        frames = self.peer.build_v6_fragments(
            IP6, VHOST6, 17, pkt, 0x7003, 1452, malformed='unaligned')
        self._inject(frames)
        self.assertIsNone(_v6_recv(self.sock, 0.6))

    def test_reserved_fields_ignored_rfc8200(self):
        # RFC 8200 4.5: the reserved byte and the two reserved flag bits are
        # ignored on reception; a datagram carrying nonzero values must still
        # reassemble. Generated fragments send zero, but receivers accept them.
        sport = self._prime_and_sport()
        body = _pattern(2000)
        pkt = self._udp_packet(sport, body)
        frames = self.peer.build_v6_fragments(
            IP6, VHOST6, 17, pkt, 0x7004, 1452, malformed='reserved')
        self._inject(frames)
        self.assertEqual(_v6_recv(self.sock, 2.0), body)

    def test_offset_beyond_limit_dropped(self):
        sport = self._prime_and_sport()
        body = _pattern(2000)
        pkt = self._udp_packet(sport, body)
        frames = self.peer.build_v6_fragments(
            IP6, VHOST6, 17, pkt, 0x7005, 1452, malformed='oversized')
        self._inject(frames)
        self.assertIsNone(_v6_recv(self.sock, 0.6))

    def test_missing_tail_then_late_tail(self):
        sport = self._prime_and_sport()
        body, frames = self._clean_datagram(sport, 2000, ident=0x7006)
        self._inject(frames[:-1])  # everything except the final fragment
        self.assertIsNone(_v6_recv(self.sock, 0.6))
        self._inject(frames[-1:])  # tail arrives later
        self.assertEqual(_v6_recv(self.sock, 2.0), body)

    def test_independent_flow_ids_both_deliver(self):
        sport = self._prime_and_sport()
        body1, f1 = self._clean_datagram(sport, 1600, ident=0x7010)
        body2, f2 = self._clean_datagram(sport, 2400, ident=0x7011)
        # Interleave the two fragment sets.
        interleaved = [x for pair in zip(f1, f2) for x in pair]
        interleaved += f1[len(f2):] + f2[len(f1):]
        self._inject(interleaved)
        got = {_v6_recv(self.sock, 2.0) for _ in range(2)}
        self.assertEqual(got, {body1, body2})

    def test_truncated_fragment_headers_dropped(self):
        sport = self._prime_and_sport()
        body = _pattern(2000)
        pkt = self._udp_packet(sport, body)
        full = self.peer.build_v6_fragments(
            IP6, VHOST6, 17, pkt, 0x7012, 1452)
        for nbytes in (0, 1, 4, 7):
            frames = self.peer.build_v6_fragments(
                IP6, VHOST6, 17, pkt, 0x7020 + nbytes, 1452, malformed=f'short{nbytes}')
            self._inject(frames)
        self.assertIsNone(_v6_recv(self.sock, 0.8))
        # Backend still healthy after malformed input.
        clean_body, clean = self._clean_datagram(sport, 1452, ident=0x7030)
        self._inject(clean)
        self.assertEqual(_v6_recv(self.sock, 2.0), clean_body)


if __name__ == '__main__':
    unittest.main()
