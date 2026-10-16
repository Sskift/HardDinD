"""Cumulative ACK batching must preserve duplicate ACKs and stream bytes."""
import os
import signal
import socket
import time
import unittest

from backend import Backend, reserve_port
from slirp_peer import SlirpPeer, TCP_ACK, recv_exact


class VnetAcks(unittest.TestCase):
    def test_batched_data_and_duplicate_ack_feedback(self):
        for address in ('127.0.0.1', '::1'):
            with self.subTest(address=address):
                self.check_ack_batch(address)

    def check_ack_batch(self, address):
        b = Backend(batch=16, vnet=True).start()
        self.addCleanup(b.stop)
        peer = SlirpPeer(b.link_host, window_scale=4)
        peer.start()
        self.addCleanup(peer.stop)
        port = reserve_port(address)
        guest = 'fd00::15' if ':' in address else '10.0.2.15'
        b.ok(f'add tcp {address} {port} 7777 {guest}')
        with socket.create_connection((address, port), timeout=5) as host:
            host.sendall(b'ready')
            self.assertEqual(recv_exact(host, 5), b'ready')
            session = next(iter(peer.sessions.values()))
            before = b.command('stats')
            body = bytes(range(256)) * 2
            os.kill(b.proc.pid, signal.SIGSTOP)
            try:
                self.assertTrue(os.WIFSTOPPED(os.waitpid(b.proc.pid, os.WUNTRACED)[1]))
                for _ in range(16):
                    session.send_segment(TCP_ACK, body)
            finally:
                os.kill(b.proc.pid, signal.SIGCONT)
            self.assertEqual(recv_exact(host, len(body) * 16), body * 16)
            self.assertGreater(b.command('stats')['coalesced_acks'], before['coalesced_acks'])
            # Host delivery precedes the peer thread consuming its cumulative
            # ACK. Drain that phase before checking duplicate-ACK feedback.
            next_seq = session.snd_nxt
            deadline = time.monotonic() + 3
            while not any(p['ack'] == next_seq for p in peer.tcp_headers) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(any(p['ack'] == next_seq for p in peer.tcp_headers), peer.tcp_headers)
            peer.tcp_headers.clear()
            before = b.command('stats')
            with session.send_lock:
                next_seq = session.snd_nxt
                os.kill(b.proc.pid, signal.SIGSTOP)
                try:
                    self.assertTrue(os.WIFSTOPPED(os.waitpid(b.proc.pid, os.WUNTRACED)[1]))
                    for _ in range(3):
                        session.snd_nxt = (next_seq - len(body)) & 0xffffffff
                        session.send_segment(TCP_ACK, body)
                finally:
                    session.snd_nxt = next_seq
                    os.kill(b.proc.pid, signal.SIGCONT)
            deadline = time.monotonic() + 3
            while len(peer.tcp_headers) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            acks = [p for p in peer.tcp_headers if p['flags'] == TCP_ACK and not p['payload']]
            self.assertGreaterEqual(len(acks), 3, acks)
            self.assertTrue(all(p['ack'] == next_seq for p in acks), acks)
            after = b.command('stats')
            self.assertEqual(after['coalesced_acks'], before['coalesced_acks'])
            self.assertEqual(after['dropped_packets'], 0)
