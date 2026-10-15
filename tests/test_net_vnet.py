import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent / 'publish'))
from slirp_peer import csum, window_option
from vnet_peer import VnetPeer


class Link:
    def send(self, data):
        self.data = data
        return len(data)

    def recv(self, size):
        return self.data[:size]


class VnetPeerTests(unittest.TestCase):
    def test_window_option_padding_and_truncation(self):
        self.assertEqual(window_option(bytes([2, 4, 5, 180, 1, 3, 3, 5])), 5)
        for data in (b'\x03', b'\x03\x03', b'\x03\x00', b'\x00\x03\x03\x05'):
            self.assertIsNone(window_option(data))

    def test_arp_is_framed_without_offload(self):
        link = Link()
        peer = VnetPeer(link)
        frame = bytes(12) + b'\x08\x06' + bytes(28)
        peer.sendall(frame)
        self.assertEqual(link.data, bytes(10) + frame)

    def test_ipv4_ipv6_partial_checksum_seed(self):
        for version in (4, 6):
            with self.subTest(version=version):
                data = b'net-gso-test' * 600
                tcp = bytearray(struct.pack('!HHIIBBHHH', 80, 30000, 123, 456,
                                            0x50, 0x18, 65535, 0, 0) + data)
                if version == 4:
                    addresses = bytes([10, 0, 2, 15, 10, 0, 2, 2])
                    ip = struct.pack('!BBHHHBBH', 0x45, 0, len(tcp) + 20, 1, 0x4000, 64, 6, 0) + addresses
                    ip = ip[:10] + struct.pack('!H', csum(ip)) + ip[12:]
                    pseudo = addresses + struct.pack('!BBH', 0, 6, len(tcp))
                else:
                    addresses = bytes.fromhex('fd000000000000000000000000000015fd000000000000000000000000000002')
                    ip = struct.pack('!IHBB', 0x60000000, len(tcp), 6, 64) + addresses
                    pseudo = addresses + struct.pack('!I3xB', len(tcp), 6)
                tcp[16:18] = struct.pack('!H', csum(pseudo + tcp))
                frame = bytes(12) + struct.pack('!H', 0x800 if version == 4 else 0x86dd) + ip + tcp
                link = Link(); peer = VnetPeer(link); peer.sendall(frame)
                flags, kind, header, size, start, offset = struct.unpack('<BBHHHH', link.data[:10])
                self.assertEqual((flags, kind, size, offset), (1, 1 if version == 4 else 4, 1460, 16))
                self.assertEqual(header, start + 20)
                # Complete the partial sum as a receiver would, then validate
                # independently against the full TCP pseudoheader.
                partial = bytearray(link.data[10 + start:])
                partial[16:18] = struct.pack('!H', csum(partial))
                self.assertEqual(csum(pseudo + partial), 0)
                self.assertEqual(VnetPeer(link).recv(65536), frame)


if __name__ == '__main__':
    unittest.main()
