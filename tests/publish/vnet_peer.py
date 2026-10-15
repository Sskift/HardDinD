"""Virtio framing adapter for the existing checksum-validating L2 peer."""
import struct

from slirp_peer import csum


class VnetPeer:
    def __init__(self, link):
        self.link = link
        self.gso_sent = self.gso_received = 0
        self.gso_sizes = []

    def __getattr__(self, name):
        return getattr(self.link, name)

    def send(self, frame):
        header = bytes(10)
        ethertype = frame[12:14]
        tcp = 34 if ethertype == b'\x08\x00' and frame[23] == 6 else 0
        if ethertype == b'\x86\xdd' and frame[20] == 6:
            tcp = 54
        if tcp:
            payload = tcp + (frame[tcp + 12] >> 4) * 4
            if len(frame) - payload > 1460:
                pseudo = (frame[26:34] + struct.pack('!BBH', 0, 6, len(frame) - tcp)
                          if tcp == 34 else
                          frame[22:54] + struct.pack('!I3xB', len(frame) - tcp, 6))
                seed = (~csum(pseudo)) & 65535
                frame = frame[:tcp + 16] + struct.pack('!H', seed) + frame[tcp + 18:]
                header = struct.pack('<BBHHHH', 1, 1 if tcp == 34 else 4,
                                     payload, 1460, tcp, 16)
                self.gso_sent += 1
        return self.link.send(header + frame) - 10

    def sendall(self, frame):
        if self.send(frame) != len(frame):
            raise OSError('short seqpacket write')

    def recv(self, size):
        data = self.link.recv(size + 10)
        if not data:
            return data
        if len(data) < 10:
            raise ValueError('short virtio header')
        if data[1]:
            self.gso_received += 1
            if len(self.gso_sizes) < 256:
                self.gso_sizes.append(struct.unpack_from('<H', data, 4)[0])
        frame = data[10:]
        if data[0] & 1:
            _, _, _, _, start, offset = struct.unpack('<BBHHHH', data[:10])
            if start + offset + 2 > len(frame):
                raise ValueError('partial checksum offset exceeds frame')
            check = csum(frame[start:]) or 65535
            position = start + offset
            frame = frame[:position] + struct.pack('!H', check) + frame[position + 2:]
        if frame[12:14] == b'\x08\x00' and frame[23] == 6:
            tcp = 14 + (frame[14] & 15) * 4
            if csum(frame[14:tcp]):
                raise ValueError('invalid IPv4 checksum from backend')
            pseudo = frame[26:34] + struct.pack('!BBH', 0, 6, len(frame) - tcp)
        elif frame[12:14] == b'\x86\xdd' and frame[20] == 6:
            tcp = 54
            pseudo = frame[22:54] + struct.pack('!I3xB', len(frame) - tcp, 6)
        else:
            return frame
        if csum(pseudo + frame[tcp:]):
            raise ValueError('invalid TCP checksum from backend')
        return frame
