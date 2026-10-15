"""Minimal Ethernet L2 "guest" for live network-backend probes.

Speaks just enough ARP, NDP, IPv4/IPv6 UDP echo and TCP echo (with ordered
shutdown / half-close) over the inherited AF_UNIX seqpacket link. It does not
touch any host device; the network-backend believes it talks to the UML guest
at 10.0.2.15 / fd00::15.
"""
import socket
import struct
import threading
import time

ETH_P_IP = 0x0800
ETH_P_ARP = 0x0806
ETH_P_IPV6 = 0x86dd

MAC = bytes.fromhex('020000000215')
IP4 = bytes([10, 0, 2, 15])
IP6 = bytes.fromhex('fd00' + '0000' * 6 + '0015')

TCP_FIN, TCP_SYN, TCP_RST, TCP_PSH, TCP_ACK = 0x01, 0x02, 0x04, 0x08, 0x10
IPPROTO_FRAGMENT = 44


def csum(data):
    if len(data) % 2:
        data += b'\x00'
    total = sum(struct.unpack('!%dH' % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def l4_checksum(src, dst, proto, segment):
    if len(src) == 4:
        pseudo = src + dst + struct.pack('!BBH', 0, proto, len(segment))
    else:
        pseudo = src + dst + struct.pack('!I3xB', len(segment), proto)
    return csum(pseudo + segment) or (0xffff if proto == 17 else 0)


class TCPSession:
    def __init__(self, peer, src, dst, sport, dport, iss, client_isn):
        self.peer = peer
        self.v6 = len(src) == 16
        self.src, self.dst, self.sport, self.dport = src, dst, sport, dport
        self.iss = iss & 0xffffffff
        self.snd_nxt = self.iss  # advanced by the SYN/FIN sent on this socket
        self.rcv_nxt = (client_isn + 1) & 0xffffffff
        self.state = 'SYN_RCVD'
        self.closed = False

    def send_segment(self, flags, payload=b''):
        pure_syn = bool(flags & TCP_SYN)
        seq = self.iss if pure_syn else self.snd_nxt
        ack = self.rcv_nxt if flags & TCP_ACK else 0
        head = struct.pack('!HHIIBBHHH', self.dport, self.sport, seq, ack,
                           0x50, flags, 0xffff, 0, 0)
        segment = head + payload
        # Reply sources from the guest (dst of the inbound frame) to the
        # router/peer (src); the TCP pseudo-header must match that direction.
        check = l4_checksum(self.dst, self.src, 6, segment)
        segment = segment[:16] + struct.pack('!H', check) + segment[18:]
        if self.v6 and self.peer.atomic_tcp:
            self.peer.send_atomic_fragment(self.dst, self.src, 6, segment)
        else:
            self.peer.send_ip(self.dst, self.src, 6, segment)
        self.snd_nxt = (self.snd_nxt + len(payload) +
                        (1 if flags & (TCP_SYN | TCP_FIN) else 0)) & 0xffffffff

    def handle(self, flags, seq, payload):
        if flags & TCP_SYN and self.state == 'SYN_RCVD':
            self.send_segment(TCP_SYN | TCP_ACK)  # retransmitted SYN
            return
        if flags & TCP_RST:
            self.state = 'CLOSED'
            self.closed = True
            return
        if not (flags & TCP_ACK):
            return
        if self.state == 'SYN_RCVD':
            self.state = 'ESTABLISHED'
        if payload and seq == self.rcv_nxt:
            self.rcv_nxt = (self.rcv_nxt + len(payload)) & 0xffffffff
            self.send_segment(TCP_ACK)
            # Segment at slirp's MSS with proper sequence advancement; PSH on
            # the last segment only.
            for i in range(0, len(payload), self.peer.mss):
                chunk = payload[i:i + self.peer.mss]
                last = i + self.peer.mss >= len(payload)
                self.send_segment((TCP_PSH if last else 0) | TCP_ACK, payload=chunk)
        elif payload:
            self.send_segment(TCP_ACK)  # gapped/retransmitted data
        if flags & TCP_FIN and seq == self.rcv_nxt:
            self.rcv_nxt = (self.rcv_nxt + 1) & 0xffffffff
            self.send_segment(TCP_ACK)
            if self.state != 'CLOSE_WAIT':
                self.state = 'CLOSE_WAIT'
                self.send_segment(TCP_FIN | TCP_ACK)
                self.state = 'LAST_ACK'
        elif flags & TCP_FIN:
            self.send_segment(TCP_ACK)
        if self.state == 'LAST_ACK' and flags & TCP_ACK:
            self.state = 'CLOSED'
            self.closed = True


class V6FragReassembler:
    """Minimal RFC 8200 reassembler used by the model guest to reassemble
    slirp's fragmented datagrams. Mirrors the policy of the pinned libslirp
    patch: first-arrived data wins, conflicting overlaps drop the queue."""

    TTL = 3.0

    def __init__(self):
        self.queues = {}
        self.next_id = 0x4000

    def _alloc(self, src, dst, ident):
        self.queues[(src, dst, ident)] = {'nxt': None, 'end': None,
                                          'pieces': {}, 'at': time.monotonic()}

    def sweep(self, now=None):
        now = now or time.monotonic()
        dead = [k for k, q in self.queues.items() if now - q['at'] > self.TTL]
        for k in dead:
            del self.queues[k]

    def input(self, src, dst, packet40):
        """Returns (nxt, payload_bytes) when a datagram completes, else None."""
        if len(packet40) < 8:
            return None
        nxt, _res, off_flags = struct.unpack('!BBH', packet40[:4])
        (ident,) = struct.unpack('!I', packet40[4:8])
        more = off_flags & 1
        off8 = off_flags >> 3
        body = packet40[8:]
        if (off_flags >> 1) & 3 or _res:
            return None
        key = (src, dst, ident)
        q = self.queues.get(key)
        if off8 == 0 and not more:
            return nxt, body  # atomic fragment
        if q is None:
            q = {'nxt': None, 'end': None, 'pieces': {}, 'at': time.monotonic()}
            self.queues[key] = q
        off = off8 * 8
        if off8 == 0:
            q['nxt'] = nxt
        # Overlap policy: identical bytes ignored, conflict drops the queue.
        for poff, pdata in list(q['pieces'].items()):
            lo, hi = max(poff, off), min(poff + len(pdata), off + len(body))
            if lo < hi:
                old = pdata[lo - poff:hi - poff]
                new = body[lo - off:hi - off]
                if old != new:
                    del self.queues[key]
                    return None
        q['pieces'][off] = body
        if not more:
            q['end'] = off + len(body)
        if q['nxt'] is None or q['end'] is None:
            return None
        expect, out = 0, bytearray()
        for poff in sorted(q['pieces']):
            if poff != expect:
                return None
            out += q['pieces'][poff]
            expect = poff + len(q['pieces'][poff])
        if expect != q['end']:
            return None
        del self.queues[key]
        return q['nxt'], bytes(out)


class SlirpPeer:
    def __init__(self, link, tcp_ports=(7777,), udp_ports=(8888,), mss=1460,
                 v6_udp_reply_mtu=None, raw_v6_udp=False,
                 atomic_tcp=False, atomic_udp=False):
        self.link = link
        # When set, v6 TCP/UDP replies are wrapped in an atomic fragment header
        # (offset 0, M=0) instead of a plain next-header packet.
        self.atomic_tcp = atomic_tcp
        self.atomic_udp = atomic_udp
        self.mss = mss
        self.tcp_ports = set(tcp_ports)
        self.udp_ports = set(udp_ports)
        # When set, large v6 UDP replies are fragmented like a real Linux
        # guest on an MTU-sized uplink (reply path of the observed failure).
        self.v6_udp_reply_mtu = v6_udp_reply_mtu
        # When set, v6 UDP datagrams are received/recorded but not auto-echoed;
        # the test injects crafted fragment sets itself.
        self.raw_v6_udp = raw_v6_udp
        self.v6_reass = V6FragReassembler()
        self.inbound_v6_fragments = []  # raw slirp->guest fragments for asserts
        self.inbound_v6_udp_ports = []  # (sport, dport) of every inbound v6 UDP
        self.frag_id = 0x5000
        self.sessions = {}
        self.iss_counter = 0x1000
        self.router_mac = {4: b'\x02' * 6, 6: b'\x02' * 6}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.udp_sources = []
        self.thread = threading.Thread(target=self._run, daemon=True, name='slirp-peer')

    def start(self):
        self.thread.start()

    def send(self, frame):
        with self.lock:
            self.link.sendall(frame)

    def send_ip(self, src, dst, proto, payload, hop_limit=64):
        family = 4 if len(src) == 4 else 6
        if family == 4:
            total = 20 + len(payload)
            hdr = struct.pack('!BBHHHBBH4s4s', 0x45, 0, total, 0, 0x4000, 64,
                              proto, 0, src, dst)
            hdr = hdr[:10] + struct.pack('!H', csum(hdr)) + hdr[12:]
            ip = hdr + payload
        else:
            ip = struct.pack('!IHBB', 0x60000000, len(payload), proto, hop_limit) + src + dst + payload
        ethertype = ETH_P_IP if family == 4 else ETH_P_IPV6
        self.send(self.router_mac[family] + MAC + struct.pack('!H', ethertype) + ip)

    def stop(self):
        self.stopping.set()
        try:
            self.link.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(timeout=2)

    def _run(self):
        while not self.stopping.is_set():
            try:
                frame = self.link.recv(65536)
            except OSError:
                return
            if not frame:
                return
            try:
                self.handle_frame(frame)
            except Exception:
                import traceback, os
                if os.environ.get('PEER_DEBUG'):
                    traceback.print_exc()
            self.v6_reass.sweep()
            dead = [k for k, s in self.sessions.items() if s.closed]
            for k in dead:
                del self.sessions[k]

    def handle_frame(self, frame):
        if len(frame) < 14:
            return
        dst_mac, src_mac = frame[:6], frame[6:12]
        if dst_mac != MAC and dst_mac != b'\xff' * 6 and not dst_mac.startswith(b'\x33\x33'):
            return
        eth_type = struct.unpack('!H', frame[12:14])[0]
        data = frame[14:]
        if eth_type == ETH_P_ARP:
            self.handle_arp(data, src_mac)
        elif eth_type == ETH_P_IP:
            self.router_mac[4] = src_mac
            self.handle_ip4(data)
        elif eth_type == ETH_P_IPV6:
            self.router_mac[6] = src_mac
            self.handle_ip6(data)

    def handle_arp(self, data, shost):
        if len(data) < 28:
            return
        htype, ptype, hlen, plen, oper = struct.unpack('!HHBBH', data[:8])
        if oper != 1 or data[24:28] != IP4:
            return
        sip, tip = data[14:18], data[24:28]
        # Reply: <tip> is-at <our MAC>, answering the requester <sip>.
        reply = struct.pack('!HHBBH', htype, ptype, hlen, plen, 2) + MAC + tip + shost + sip
        self.send(shost + MAC + struct.pack('!H', ETH_P_ARP) + reply)

    def handle_ip4(self, data):
        if len(data) < 20 or csum(data[:(data[0] & 0x0f) * 4]) != 0:
            return
        ihl = (data[0] & 0x0f) * 4
        src, dst = data[12:16], data[16:20]
        if dst != IP4:
            return
        payload = data[ihl:]
        if data[9] == 17:
            self.handle_udp(src, dst, payload, 4)
        elif data[9] == 6:
            self.handle_tcp(src, dst, payload, 4)

    def handle_ip6(self, data):
        if len(data) < 40:
            return
        src, dst, nxt = data[8:24], data[24:40], data[6]
        if dst != IP6 and nxt != 58:
            return
        if nxt == IPPROTO_FRAGMENT:
            self.inbound_v6_fragments.append(data)
            done = self.v6_reass.input(bytes(src), bytes(dst), data[40:])
            if done is None:
                return
            nxt, payload = done
            if nxt == 17:
                self.handle_udp(src, dst, payload, 6)
            elif nxt == 6:
                self.handle_tcp(src, dst, payload, 6)
            return
        if nxt not in (6, 17, 58):
            return
        payload = data[40:]
        if nxt == 58:
            self.handle_ndp(src, payload)
        elif nxt == 17:
            self.handle_udp(src, dst, payload, 6)
        else:
            self.handle_tcp(src, dst, payload, 6)

    def handle_ndp(self, src, payload):
        if len(payload) < 24 or payload[0] != 135 or payload[8:24] != IP6:
            return
        na = struct.pack('!BBHI', 136, 0, 0, 0x60) + IP6 + bytes([2, 1]) + MAC
        check = l4_checksum(IP6, src, 58, na)
        na = na[:4] + struct.pack('!H', check) + na[6:]
        # RFC 4861: NDP packets use hop limit 255, which slirp validates.
        self.send_ip(IP6, src, 58, na, hop_limit=255)

    def send_atomic_fragment(self, src, dst, nxt, packet):
        """Emit packet as an atomic IPv6 fragment (offset 0, M=0)."""
        fh = struct.pack('!BBHI', nxt, 0, 0, self.frag_id)
        self.frag_id = (self.frag_id + 1) & 0xffffffff
        self.send_ip(src, dst, IPPROTO_FRAGMENT, fh + packet)

    def send_ip6_fragmented(self, src, dst, nxt, packet):
        """Emit an IPv6 packet as RFC 8200 fragments at self.v6_udp_reply_mtu.
        The upper-layer checksum in the first fragment covers the original
        pseudo-header/length and is preserved byte-for-byte."""
        mtu = self.v6_udp_reply_mtu
        if mtu is None or 40 + len(packet) <= mtu:
            self.send_ip(src, dst, nxt, packet)
            return
        chunk_max = (mtu - 48) & ~7
        ident = self.frag_id
        self.frag_id = (self.frag_id + 1) & 0xffffffff
        offset = 0
        while offset < len(packet):
            chunk = packet[offset:offset + chunk_max]
            more = 0 if offset + len(chunk) == len(packet) else 1
            fh = struct.pack('!BBHI', nxt, 0, ((offset // 8) << 3) | more, ident)
            self.send_ip(src, dst, IPPROTO_FRAGMENT, fh + chunk)
            offset += len(chunk)

    def inject_eth(self, frame):
        """Send a raw crafted Ethernet frame into slirp (test use)."""
        self.send(frame)

    def build_v6_fragments(self, src, dst, nxt, packet, ident, chunk_size,
                          malformed=None):
        """Craft raw fragment frames for adversarial reassembly tests.
        malformed: 'unaligned', 'reserved', 'oversized', 'shortN' truncations."""
        chunk_size = chunk_size & ~7
        frames = []
        offsets = list(range(0, len(packet), chunk_size))

        def frame_at(offset, raw_len, nxt_byte=nxt, reserved=0, res_byte=0,
                     id_override=None, drop_align=False):
            more = 0 if offset + raw_len >= len(packet) else 1
            off_flags = ((offset // 8) << 3) | more
            if reserved:
                off_flags |= reserved
            body = packet[offset:offset + raw_len]
            if drop_align and more:
                body = body[:-1]  # non-final, not 8-aligned
            fh = struct.pack('!BBHI', nxt_byte, res_byte, off_flags,
                             ident if id_override is None else id_override)
            # IPv6 payload length excludes the 40-byte fixed header.
            pkt = struct.pack('!IHBB', 0x60000000, len(fh) + len(body), 44, 64)
            pkt += src + dst + fh + body
            eth = self.router_mac[6] + MAC + struct.pack('!H', ETH_P_IPV6) + pkt
            return eth

        for i, offset in enumerate(offsets):
            raw_len = min(chunk_size, len(packet) - offset)
            frames.append(frame_at(offset, raw_len))
        if malformed == 'unaligned' and frames:
            # Rebuild middle/last pieces with a non-aligned non-final chunk.
            frames = [frame_at(0, min(chunk_size, len(packet)), drop_align=True)]
        if malformed == 'reserved' and frames:
            # Every fragment carries nonzero reserved byte and reserved flag
            # bits; RFC 8200 receivers must ignore them and reassemble.
            frames = [frame_at(offset, min(chunk_size, len(packet) - offset),
                              reserved=2, res_byte=0xAB)
                      for offset in offsets]
        if malformed == 'oversized':
            big_off = 8184  # 8184*8 + payload > 65495
            pkt_body = b'X' * 100
            off_flags = (big_off << 3) | 1
            fh = struct.pack('!BBHI', nxt, 0, off_flags, ident)
            pkt = struct.pack('!IHBB', 0x60000000, len(fh) + len(pkt_body), 44, 64)
            pkt += src + dst + fh + pkt_body
            frames = [self.router_mac[6] + MAC + struct.pack('!H', ETH_P_IPV6) + pkt]
        if isinstance(malformed, str) and malformed.startswith('short'):
            nbytes = int(malformed[5:])
            full = frames[0]
            frames = [full[:14 + 40 + nbytes]]
        if malformed == 'identicalpartial':
            # First fragment [0, chunk_size) with M=1; a second fragment starts
            # 8 bytes in and re-sends the same overlapping bytes plus more.
            # RFC 8200: only exact duplicates are tolerated, so this drops the
            # whole datagram even though all overlapping bytes are identical.
            f0 = frame_at(0, chunk_size, reserved=0)
            body1 = packet[8:8 + chunk_size]
            fh1 = struct.pack('!BBHI', nxt, 0, (1 << 3) | 1, ident)
            pkt1 = struct.pack('!IHBB', 0x60000000, len(fh1) + len(body1), 44, 64)
            pkt1 += src + dst + fh1 + body1
            f1 = self.router_mac[6] + MAC + struct.pack('!H', ETH_P_IPV6) + pkt1
            frames = [f0, f1]
        if malformed == 'conflictingfirst':
            # Two different first fragments (offset 0, M=1) for one flow.
            f0 = frame_at(0, chunk_size, reserved=0)
            alt = bytes(b ^ 0xff for b in packet[:chunk_size])
            f1 = frame_at(0, chunk_size, reserved=0)
            # Rebuild f1 with mutated body by constructing manually.
            fh1 = struct.pack('!BBHI', nxt, 0, 1, ident)
            pkt1 = struct.pack('!IHBB', 0x60000000, len(fh1) + len(alt), 44, 64)
            pkt1 += src + dst + fh1 + alt
            f1 = self.router_mac[6] + MAC + struct.pack('!H', ETH_P_IPV6) + pkt1
            frames = [f0, f1]
        return frames

    def handle_udp(self, src, dst, payload, family):
        if len(payload) < 8:
            return
        sport, dport, length, _ = struct.unpack('!HHHH', payload[:8])
        if dport not in self.udp_ports or length < 8 or length > len(payload):
            return
        body = payload[8:length]
        self.udp_sources.append((family, bytes(src), sport))
        if family == 6:
            self.inbound_v6_udp_ports.append((sport, dport))
        if family == 6 and self.raw_v6_udp:
            return  # tests inject crafted replies themselves
        out = struct.pack('!HHHH', dport, sport, 8 + len(body), 0) + body
        check = l4_checksum(dst, src, 17, out)
        out = out[:6] + struct.pack('!H', check) + out[8:]
        if family == 6 and self.atomic_udp:
            self.send_atomic_fragment(dst, src, 17, out)
        elif family == 6 and self.v6_udp_reply_mtu is not None:
            self.send_ip6_fragmented(dst, src, 17, out)
        else:
            self.send_ip(dst, src, 17, out)

    def handle_tcp(self, src, dst, payload, family):
        if len(payload) < 20:
            return
        sport, dport, seq, ack, off_res, flags, win, check, urg = struct.unpack(
            '!HHIIBBHHH', payload[:20])
        if dport not in self.tcp_ports:
            return
        off = (off_res >> 4) * 4
        body = payload[off:]
        key = (family, bytes(src), bytes(dst), sport, dport)
        sess = self.sessions.get(key)
        if sess is None:
            if not (flags & TCP_SYN) or (flags & TCP_ACK):
                return
            self.iss_counter += 1
            sess = TCPSession(self, src, dst, sport, dport, 0xbd000000 + self.iss_counter, seq)
            self.sessions[key] = sess
            sess.send_segment(TCP_SYN | TCP_ACK)
            return
        sess.handle(flags, seq, body)
