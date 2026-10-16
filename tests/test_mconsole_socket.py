"""Exercise real Unix datagrams, including the long-path shutdown failure."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import instance as harddind

SERVER = r'''
import socket, struct, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
    s.bind('mconsole')
    s.settimeout(5)
    print('ready', flush=True)
    request, address = s.recvfrom(1024)
    magic, version, length, data = struct.unpack('=III512s', request)
    assert (magic, version, data[:length]) == (0xcafebabe, 2, b'cad')
    message = b'rejected' if int(sys.argv[1]) else b''
    s.sendto(struct.pack('=III', int(sys.argv[1]), 0, len(message) + 1) + message + b'\0', address)
'''


@unittest.skipUnless(sys.platform == 'linux', 'runtime requires Linux /proc fd paths')
class MconsoleSocketTests(unittest.TestCase):
    def roundtrip(self, long_path, reject=False):
        with tempfile.TemporaryDirectory(prefix='hd-mc-', dir='/tmp') as td:
            instance = Path(td)
            parent = instance / ('x' * 90 if long_path else 'uml') / '123456789abc'
            parent.mkdir(parents=True)
            path = parent / 'mconsole'
            if long_path:
                self.assertGreater(len(os.fsencode(path)), 107)
            before_cwd = os.getcwd()
            before_fds = set(os.listdir('/proc/self/fd'))
            with subprocess.Popen([sys.executable, '-c', SERVER, str(int(reject))],
                                  cwd=parent, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as server:
                try:
                    self.assertEqual(server.stdout.readline(), 'ready\n')
                    if reject:
                        with self.assertRaisesRegex(RuntimeError, 'rejected'):
                            harddind.mconsole(instance, path, 'cad')
                    else:
                        harddind.mconsole(instance, path, 'cad')
                    out, err = server.communicate(timeout=5)
                    self.assertEqual(server.returncode, 0, (out, err))
                finally:
                    if server.poll() is None:
                        server.kill()
                        server.communicate()
            self.assertEqual(os.getcwd(), before_cwd)
            self.assertEqual(set(os.listdir('/proc/self/fd')), before_fds)
            self.assertEqual(list(instance.glob('m-*')), [])

    def test_long_path(self):
        self.roundtrip(True)

    def test_short_existing_path(self):
        self.roundtrip(False)

    def test_driver_error_cleanup(self):
        self.roundtrip(True, reject=True)


if __name__ == '__main__':
    unittest.main()
