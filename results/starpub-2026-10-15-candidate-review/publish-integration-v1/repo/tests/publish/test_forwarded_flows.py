"""End-to-end forwarded-flow probes through the real network-backend:

TCP echo (with half-close) and UDP echo over both IPv4 and IPv6, using the
minimal L2 peer as the "guest". Everything runs over loopback ephemeral ports
in disposable subprocesses; no host device or namespace is touched.
"""
import os
import sys
import socket
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from backend import Backend, reserve_port
from slirp_peer import SlirpPeer


def tcp_echo(addr, port, size, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((addr, port), timeout=timeout) as s:
                s.settimeout(timeout)
                payload = bytes((i * 7 + 3) & 0xff for i in range(size))
                s.sendall(payload)
                received = bytearray()
                while len(received) < size:
                    part = s.recv(65536)
                    if not part:
                        raise RuntimeError('early EOF before full echo')
                    received.extend(part)
                if received != payload:
                    raise RuntimeError('payload mismatch')
                # Half-close: server must finish echoing, then signal EOF.
                s.shutdown(socket.SHUT_WR)
                tail = s.recv(65536)
                if tail:
                    raise RuntimeError('unexpected data after half-close')
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def udp_echo(addr, port):
    family = socket.AF_INET6 if ':' in addr else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.settimeout(5)
        s.connect((addr, port))
        for size in (1, 512, 1400):
            payload = bytes((i * 13 + 1) & 0xff for i in range(size))
            s.send(payload)
            data, who = s.recvfrom(65535)
            if data != payload:
                raise RuntimeError('udp payload mismatch')
            if who[:2] != (addr, port):
                raise RuntimeError(f'udp reply endpoint drifted: {who}')


class ForwardedFlows(unittest.TestCase):
    def setUp(self):
        self.b = Backend().start()
        self.peer = SlirpPeer(self.b.link_host)
        self.peer.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.peer.stop()
        self.b.stop()

    def command_ok(self, command):
        reply = self.b.command(command)
        if reply.get('ok') is not True:
            raise AssertionError(f'{command!r} -> {reply}')

    def test_tcp4_tcp6_udp4_udp6_flows_and_removal(self):
        tcp4 = reserve_port('127.0.0.1')
        tcp6 = reserve_port('::1')
        udp4 = reserve_port('127.0.0.1', udp=True)
        udp6 = reserve_port('::1', udp=True)
        self.command_ok(f'create tcp 127.0.0.1 {tcp4} 7777 10.0.2.15')
        self.command_ok(f'create tcp ::1 {tcp6} 7777 fd00::15')
        self.command_ok(f'create udp 127.0.0.1 {udp4} 8888 10.0.2.15')
        self.command_ok(f'create udp ::1 {udp6} 8888 fd00::15')

        tcp_echo('127.0.0.1', tcp4, 4096)
        tcp_echo('::1', tcp6, 4096)
        udp_echo('127.0.0.1', udp4)
        udp_echo('::1', udp6)

        # Same host port on two distinct local IPs, same guest service; slirp
        # opens two independent guest connections from distinct source ports.
        shared = reserve_port('127.0.0.1')
        self.command_ok(f'create tcp 127.0.0.1 {shared} 7777 10.0.2.15')
        self.command_ok(f'create tcp 127.0.0.2 {shared} 7777 10.0.2.15')
        tcp_echo('127.0.0.1', shared, 1200)
        tcp_echo('127.0.0.2', shared, 1200)

        # Removal tears the host sockets down; flows must fail promptly.
        self.command_ok(f'del tcp 127.0.0.1 {tcp4} 7777 10.0.2.15')
        self.command_ok(f'del udp ::1 {udp6} 8888 fd00::15')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', tcp4), timeout=1)
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(('::1', udp6))
            s.send(b'noreply')
            with self.assertRaises(OSError):
                s.recv(65535)


if __name__ == '__main__':
    unittest.main(verbosity=2)
