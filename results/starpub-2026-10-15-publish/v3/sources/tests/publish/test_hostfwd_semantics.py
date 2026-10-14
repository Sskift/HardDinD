"""Native bind/conflict/removal semantics for the real network-backend via
its unix-dgram control protocol. These probes bind real host loopback
sockets through pinned libslirp 4.6.1 and check the kernel-adjudicated
outcomes Docker must see.
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from backend import Backend, can_bind, reserve_port


class HostFwdSemantics(unittest.TestCase):
    def setUp(self):
        self.b = Backend().start()

    def tearDown(self):
        self.b.stop()

    def test_distinct_v4_addresses_share_ports_and_exact_binds_conflict(self):
        port = reserve_port('127.0.0.1')
        self.b.ok(f'create tcp 127.0.0.1 {port} 7777 10.0.2.15')
        # Same address/port conflicts even with SO_REUSEADDR (active listener).
        self.assertFalse(can_bind('127.0.0.1', port))
        # Same port on a distinct local IP must remain bindable.
        self.assertTrue(can_bind('127.0.0.2', port))
        # Backend itself accepts the distinct-IP mapping.
        self.b.ok(f'create tcp 127.0.0.2 {port} 7778 10.0.2.16')
        self.b.fail(f'create tcp 127.0.0.1 {port} 7779 10.0.2.17')
        rows = self.b.list()
        self.assertEqual({r['address'] for row in [rows] for r in row}, {'127.0.0.1', '127.0.0.2'})
        self.assertTrue(all(r['family'] == 'tcp4' for r in rows))

    def test_wildcard_vs_specific_conflicts_both_orders_v4(self):
        port = reserve_port('127.0.0.1')
        self.b.ok(f'create tcp 0.0.0.0 {port} 7777 10.0.2.16')
        error = self.b.fail(f'create tcp 127.0.0.1 {port} 7777 10.0.2.17')
        self.assertIn('unavailable', error)  # kernel rejects overlap with wildcard
        # Kernel also blocks the inverse overlap even without table hits.
        port2 = reserve_port('127.0.0.1')
        self.b.ok(f'create tcp 127.0.0.1 {port2} 7777 10.0.2.17')
        error = self.b.fail(f'create tcp 0.0.0.0 {port2} 7777 10.0.2.16')
        self.assertIn('unavailable', error)

    def test_dual_stack_wildcards_coexist_and_v6_bind_is_v6only(self):
        port = reserve_port('::1')
        # IPv6 wildcard alone: same-port v4 bind must still work (V6ONLY).
        self.b.ok(f'create tcp :: {port} 7777 fd00::10')
        self.assertTrue(can_bind('127.0.0.1', port))
        self.assertFalse(can_bind('::1', port, v6only=1))
        # v4 wildcard then coexists with the v6 one (families independent).
        self.b.ok(f'create tcp 0.0.0.0 {port} 7777 10.0.2.16')
        self.assertFalse(can_bind('127.0.0.1', port))
        rows = self.b.list()
        self.assertEqual(sorted(r['family'] for r in rows), ['tcp4', 'tcp6'])

    def test_specific_v4_and_v6_loopbacks_coexist(self):
        port = reserve_port('127.0.0.1')
        self.b.ok(f'create tcp 127.0.0.1 {port} 7777 10.0.2.15')
        self.b.ok(f'create tcp ::1 {port} 7777 fd00::15')
        self.assertTrue(can_bind('127.0.0.2', port))
        self.assertFalse(can_bind('::1', port, v6only=1))
        # ::1 specific vs wildcard :: same port conflicts.
        self.b.fail(f'create tcp :: {port} 7777 fd00::10')
        rows = self.b.list()
        self.assertEqual(sorted(r['family'] for r in rows), ['tcp4', 'tcp6'])

    def test_tcp_and_udp_are_independent(self):
        port = reserve_port('127.0.0.1')
        self.b.ok(f'create tcp 127.0.0.1 {port} 7777 10.0.2.15')
        self.b.ok(f'create udp 127.0.0.1 {port} 7777 10.0.2.15')
        rows = self.b.list()
        self.assertEqual(sorted(r['protocol'] for r in rows), ['tcp', 'udp'])
        # UDP wildcard vs specific is kernel-enforced (no REUSEPORT involved).
        self.b.fail(f'create udp 0.0.0.0 {port} 7778 10.0.2.16')
        # UDP v6 on the same port is a different family and binds fine.
        self.b.ok(f'create udp ::1 {port} 7777 fd00::15')

    def test_udp_distinct_specific_addresses_share_port(self):
        port = reserve_port('127.0.0.1', udp=True)
        self.b.ok(f'create udp 127.0.0.1 {port} 8888 10.0.2.15')
        self.b.ok(f'create udp 127.0.0.2 {port} 8888 10.0.2.16')
        # Exact duplicate is stopped by the backend table even though slirp
        # sets SO_REUSEADDR for UDP sockets.
        self.b.fail(f'create udp 127.0.0.1 {port} 8889 10.0.2.17')

    def test_create_add_del_lifecycle(self):
        port = reserve_port('127.0.0.1')
        self.b.ok(f'create tcp 127.0.0.1 {port} 7777 10.0.2.15')
        self.b.fail(f'create tcp 127.0.0.1 {port} 7777 10.0.2.15')
        # add is idempotent for the identical mapping.
        self.b.ok(f'add tcp 127.0.0.1 {port} 7777 10.0.2.15')
        self.b.fail(f'add tcp 127.0.0.1 {port} 7778 10.0.2.15')
        # deleting a missing mapping succeeds like the v4 grammar.
        self.b.ok(f'del tcp 127.0.0.1 {port + 1} 7777 10.0.2.15')
        self.b.fail(f'del tcp 127.0.0.1 {port} 9999 10.0.2.15')
        self.b.ok(f'del tcp 127.0.0.1 {port} 7777 10.0.2.15')
        self.assertEqual(self.b.list(), [])
        # The port is released promptly.
        deadline = time.monotonic() + 2
        while not can_bind('127.0.0.1', port) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(can_bind('127.0.0.1', port))

    def test_ipv6_lifecycle_and_legacy_five_field_default(self):
        port = reserve_port('::1')
        self.b.ok(f'create tcp ::1 {port} 7777 fd00::15')
        self.b.fail(f'create tcp ::1 {port} 7777 fd00::15')
        self.b.ok(f'del tcp ::1 {port} 7777 fd00::15')
        # Historical 5-field grammar keeps defaulting v4 to 10.0.2.15.
        port2 = reserve_port('127.0.0.1')
        self.b.ok(f'add tcp 127.0.0.1 {port2} 2375')
        rows = self.b.list()
        self.assertEqual(rows[0]['guest_address'], '10.0.2.15')

    def test_invalid_families_are_rejected(self):
        self.b.fail('create tcp 999.1.1.1 9000 9000 10.0.2.15')
        self.b.fail('create tcp 127.0.0.1 9000 9000 ::1')
        self.b.fail('create tcp ::ffff:127.0.0.1 9000 9000 ::1')
        self.b.fail('create tcp fe80::1%lo 9000 9000 fd00::15')
        self.b.fail('create sctp 127.0.0.1 9000 9000 10.0.2.15')
        self.b.fail('create tcp 127.0.0.1 70000 9000 10.0.2.15')

    def test_stats_reports_forwards_and_shutdown_closes_sockets(self):
        port = reserve_port('127.0.0.1')
        self.b.ok(f'create udp 127.0.0.1 {port} 7777 10.0.2.15')
        stats = self.b.command('stats')
        self.assertEqual(stats['forwards'], 1)
        self.b.stop()
        deadline = time.monotonic() + 2
        while not can_bind('127.0.0.1', port, udp=True) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(can_bind('127.0.0.1', port, udp=True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
