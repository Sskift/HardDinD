"""Minimal Ethernet L2 "guest" for live network-backend probes.

Speaks just enough ARP, NDP, IPv4/IPv6 UDP echo and TCP echo (with ordered
shutdown / half-close) over the inherited AF_UNIX seqpacket link. It does not
touch any host device; the network-backend believes it talks to the UML guest
at 10.0.2.15 / fd00::15.
"""
import socket
import struct
import threading

ETH_P_IP = 0x0800
ETH_P_ARP = 0x0806
ETH_P_IPV6 = 0x86dd

MAC = bytes.fromhex('020000000215')
IP4 = bytes([10, 0, 2, 15])
IP6 = bytes.fromhex('fd00' + '0000' * 6 + '0015')

TCP_FIN, TCP_SYN, TCP_RST, TCP_PSH, TCP_ACK = 0x01, 0x02, 0x04, 0x08, 0x10


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


class SlirpPeer:
    def __init__(self, link, tcp_ports=(7777,), udp_ports=(8888,), mss=1460):
        self.link = link
        self.mss = mss
        self.tcp_ports = set(tcp_ports)
        self.udp_ports = set(udp_ports)
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
        if len(data) < 40 or data[6] not in (6, 17, 58):
            return
        src, dst, nxt = data[8:24], data[24:40], data[6]
        if dst != IP6 and nxt != 58:
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

    def handle_udp(self, src, dst, payload, family):
        if len(payload) < 8:
            return
        sport, dport, length, _ = struct.unpack('!HHHH', payload[:8])
        if dport not in self.udp_ports or length < 8 or length > len(payload):
            return
        body = payload[8:length]
        out = struct.pack('!HHHH', dport, sport, 8 + len(body), 0) + body
        check = l4_checksum(dst, src, 17, out)
        out = out[:6] + struct.pack('!H', check) + out[8:]
        self.udp_sources.append((family, bytes(src), sport))
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
