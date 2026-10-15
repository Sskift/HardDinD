import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from backend import Backend, reserve_port
from slirp_peer import SlirpPeer
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
        self.assertGreater(stats['coalesced'], 0)
        self.assertEqual(stats['dropped_packets'], 0)

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

if __name__ == '__main__':
    unittest.main()
