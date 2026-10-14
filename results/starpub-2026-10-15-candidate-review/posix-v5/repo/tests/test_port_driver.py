import importlib.util
import io
import json
import pathlib
import socket
import unittest

spec = importlib.util.spec_from_file_location('port_driver', pathlib.Path(__file__).resolve().parents[1] / 'scripts/port_driver.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PortDriverTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.failure = None
        def control(command):
            self.commands.append(command)
            if self.failure: raise RuntimeError(self.failure)
        self.server, self.client = socket.socketpair()
        self.client.settimeout(2)
        self.driver = module.PortDriver(self.server, control, io.StringIO(), '10.0.2.3')
        self.spec = {'proto': 'tcp4', 'parentIP': '127.0.0.1', 'parentPort': 49123,
                     'childIP': '10.0.2.15', 'childPort': 49123}

    def tearDown(self):
        if self.driver.thread.ident is not None: self.driver.stop()
        else: self.server.close()
        self.client.close()

    def test_failed_creation_is_not_owned_and_failed_delete_keeps_ownership(self):
        self.failure = 'port occupied'
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            self.driver.handle('POST', '/v1/ports', self.spec)
        self.assertFalse(self.driver.ports)
        self.failure = None
        _, status = self.driver.handle('POST', '/v1/ports', self.spec)
        self.assertTrue(self.commands[-1].startswith('create '))
        self.failure = 'backend unavailable'
        with self.assertRaises(RuntimeError):
            self.driver.handle('DELETE', f'/v1/ports/{status["id"]}', None)
        self.assertIn(status['id'], self.driver.ports)
        self.failure = None
        self.driver.handle('DELETE', f'/v1/ports/{status["id"]}', None)
        self.assertFalse(self.driver.ports)

    def test_invalid_destination_or_port_never_reaches_backend(self):
        for field, value in [('childIP', '10.0.2.2'), ('proto', 'tcp6'), ('parentPort', True),
                             ('parentPort', 0), ('childPort', 65536), ('parentIP', '::1')]:
            with self.assertRaises(ValueError):
                self.driver.handle('POST', '/v1/ports', {**self.spec, field: value})
        self.assertEqual(self.commands, [])

    def test_framed_errors_do_not_desynchronize_following_request(self):
        self.driver.start()
        frames = [dict(id=1, method='DELETE', path=None), dict(id=2, method='GET', path='/v1/info')]
        wire = b''.join(json.dumps(f).encode()+b'\n' for f in frames)
        self.client.sendall(wire[:9]); self.client.sendall(wire[9:])
        with self.client.makefile('rb') as reader:
            first, second = json.loads(reader.readline()), json.loads(reader.readline())
        self.assertEqual((first['id'], first['status']), (1, 400))
        self.assertEqual((second['id'], second['status']), (2, 200))
        self.assertEqual(second['body']['networkDriver']['childIP'], '10.0.2.15')
        self.driver.begin_shutdown(); self.client.shutdown(socket.SHUT_WR)
        self.driver.thread.join(2)
        self.assertFalse(self.driver.thread.is_alive())
        self.assertIsNone(self.driver.error)

    def test_unexpected_disconnect_is_failure(self):
        self.driver.start(); self.client.shutdown(socket.SHUT_WR)
        self.driver.thread.join(2)
        self.assertEqual(self.driver.error, 'guest port-control channel closed')
