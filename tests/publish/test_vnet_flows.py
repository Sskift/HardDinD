import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from backend import Backend, reserve_port
from slirp_peer import SlirpPeer, MAC, IP4, IP6, csum
import test_forwarded_flows as flows


class VnetFlows(flows.ForwardedFlows):
    def setUp(self):
        self.b = Backend(batch=16, vnet=True).start()
        self.peer = SlirpPeer(self.b.link_host, window_scale=4)
        self.peer.mss = 60000
        self.peer.start()
        self.addCleanup(self._cleanup)

    def test_large_tcp4_tcp6_with_partial_checksums(self):
        for addr, guest in (('127.0.0.1', '10.0.2.15'), ('::1', 'fd00::15')):
            with self.subTest(address=addr):
                port = reserve_port(addr)
                self.command_ok(f'add tcp {addr} {port} 7777 {guest}')
                flows.tcp_echo(addr, port, 512 * 1024, timeout=20)
                self.command_ok(f'del tcp {addr} {port} 7777 {guest}')
        stats = self.b.command('stats')
        self.assertTrue(stats['vnet'])
        self.assertGreater(stats['rx_gso'], 0)
        self.assertGreater(stats['tx_gso'], 0)
        self.assertGreater(stats['slirp_gso'], 0)
        self.assertGreater(stats['slirp_gso_bytes'], 0)
        self.assertEqual(stats['dropped_packets'], 0)

    def test_large_segments_respect_guest_mss(self):
        for mss in (536, 1200, 1460):
            for addr, guest in (('127.0.0.1', '10.0.2.15'), ('::1', 'fd00::15')):
                with self.subTest(mss=mss, address=addr):
                    self.peer.advertised_mss = mss
                    self.b.link_host.gso_sizes.clear()
                    port = reserve_port(addr)
                    self.command_ok(f'add tcp {addr} {port} 7777 {guest}')
                    flows.tcp_echo(addr, port, 1024 * 1024 + 17, timeout=20)
                    self.command_ok(f'del tcp {addr} {port} 7777 {guest}')
                    sizes = self.b.link_host.gso_sizes
                    self.assertTrue(sizes)
                    limit = min(mss, 1460 if ':' not in addr else 1440)
                    self.assertTrue(all(size <= limit for size in sizes), sizes)
        self.assertEqual(self.b.command('stats')['dropped_packets'], 0)

    def test_invalid_vnet_headers_are_dropped(self):
        raw = self.b.link_host.link
        before = self.b.command('stats')['dropped_packets']
        raw.send(b'\x00' * 5)
        raw.send(b'\x80' + b'\x00' * 29)
        # Offset extends past this Ethernet frame.
        raw.send(bytes.fromhex('010000000000ffff1000') + bytes(54))
        # Unknown GSO type.
        raw.send(bytes.fromhex('00030000010000000000') + bytes(54))
        self.assertEqual(self.b.command('stats')['dropped_packets'] - before, 4)

    def test_udp4_udp6_egress_partial_checksum_zero(self):
        for family, address, src, dst in (
                (socket.AF_INET, '127.0.0.1', IP4, socket.inet_pton(socket.AF_INET, '10.0.2.2')),
                (socket.AF_INET6, '::1', IP6, socket.inet_pton(socket.AF_INET6, 'fd00::2'))):
            with self.subTest(address=address), socket.socket(family, socket.SOCK_DGRAM) as host:
                host.bind((address, 0)); host.settimeout(3)
                udp = struct.pack('!HHHH', 8889, host.getsockname()[1], 10, 0)
                v4 = family == socket.AF_INET
                pseudo = src + dst + (struct.pack('!BBH', 0, 17, 10) if v4 else
                                      struct.pack('!I3xB', 10, 17))
                payload = struct.pack('!H', csum(pseudo + udp))
                self.assertEqual(csum(pseudo + udp + payload), 0)
                seed = (~csum(pseudo)) & 65535
                udp = udp[:6] + struct.pack('!H', seed) + payload
                if v4:
                    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 30, 0, 0, 64, 17, 0, src, dst)
                    ip = ip[:10] + struct.pack('!H', csum(ip)) + ip[12:]
                else:
                    ip = struct.pack('!IHBB', 0x60000000, 10, 17, 64) + src + dst
                ethernet = self.peer.router_mac[4 if v4 else 6] + MAC + struct.pack('!H', 0x800 if v4 else 0x86dd)
                header = struct.pack('<BBHHHH', 1, 0, 0, 0, len(ethernet + ip), 6)
                self.b.link_host.link.send(header + ethernet + ip + udp)
                self.assertEqual(host.recv(100), payload)


if __name__ == '__main__':
    unittest.main()
