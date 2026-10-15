import hashlib
import importlib.util
import pathlib
import socket
import ssl
import threading
import unittest


spec = importlib.util.spec_from_file_location('local_egress', pathlib.Path(__file__).parent / 'publish/local_egress.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)


class LocalTransferTests(unittest.TestCase):
    def test_stalled_tls_handshake_times_out_before_cleanup(self):
        class TLS:
            started = threading.Event()
            timed_out = threading.Event()

            def wrap_socket(self, connection, **kwargs):
                self.started.set()
                try:
                    return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).wrap_socket(connection, **kwargs)
                except (socket.timeout, TimeoutError):
                    self.timed_out.set()
                    raise

        tls = TLS()
        with local.server(b'body', [], tls, timeout=0.1) as port:
            client = socket.create_connection(('127.0.0.1', port), timeout=1)
            self.addCleanup(client.close)
            fallback = threading.Timer(2, client.close)
            fallback.daemon = True
            fallback.start()
            self.addCleanup(fallback.cancel)
            self.assertTrue(tls.started.wait(1))
        self.assertTrue(tls.timed_out.is_set())

    def test_payload_tail_and_prefix_are_stable(self):
        large = local.payload(65536 + 17)
        self.assertEqual(len(large), 65553)
        self.assertEqual(large[:17], large[-17:])
        self.assertEqual(large[:100], local.payload(100))
        self.assertNotEqual(hashlib.sha256(large).digest(), hashlib.sha256(large[:-1]).digest())

    def test_request_chunk_rejects_invalid_paths(self):
        self.assertEqual(local.request_chunk('/guest/3'), 1048576)
        self.assertEqual(local.request_chunk('/native/4'), 1460)
        for path in ('/', '/guest/x', '/guest/1/2', '/else/1', '/native/-1'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                local.request_chunk(path)
