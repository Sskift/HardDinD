import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from backend import Backend, reserve_port
from slirp_peer import SlirpPeer, TCP_SYN
from test_forwarded_flows import tcp_echo


class TCPWindows(unittest.TestCase):
    def test_window_negotiation_and_unscaled_fallback(self):
        for vnet, peer_scale in ((False, 4), (True, None), (True, 4)):
            for address, guest in (('127.0.0.1', '10.0.2.15'), ('::1', 'fd00::15')):
                with self.subTest(vnet=vnet, scale=peer_scale, address=address):
                    backend = Backend(vnet=vnet, batch=16).start()
                    peer = SlirpPeer(backend.link_host, window_scale=peer_scale)
                    peer.start()
                    try:
                        port = reserve_port(address)
                        backend.ok(f'add tcp {address} {port} 7777 {guest}')
                        tcp_echo(address, port, 4096)
                        syn = next(h for h in peer.tcp_headers if h['flags'] & TCP_SYN)
                        self.assertEqual(syn['scale'], 5 if vnet else None)
                        self.assertEqual(syn['window'], 65535)
                        ack = next(h for h in peer.tcp_headers if not h['flags'] & TCP_SYN)
                        if vnet and peer_scale is not None:
                            self.assertTrue(32000 <= ack['window'] <= 33000, ack)
                            self.assertGreater(ack['window'] << syn['scale'], 1000000)
                        else:
                            self.assertEqual(ack['window'], 65535)
                    finally:
                        peer.stop()
                        backend.stop()


if __name__ == '__main__':
    unittest.main()
