#!/usr/bin/env python3
"""Decode optional DNS-only link traces; report packet facts, not inferred causes."""
import argparse
import ipaddress
import json
import pathlib
import struct

def checksum(data):
    data += b'\0' * (len(data) % 2)
    total = sum(struct.unpack('!' + 'H' * (len(data)//2), data))
    while total >> 16: total = (total & 65535) + (total >> 16)
    return (~total) & 65535

def decode(line):
    _, timestamp, direction, encoded = line.split()
    frame = bytes.fromhex(encoded)
    ip = frame[14:]; ihl = (ip[0] & 15) * 4
    udp = ip[ihl:]; source, dest, length, check = struct.unpack('!HHHH', udp[:8]); udp = udp[:length]
    dns = udp[8:]; ident, flags, questions, answers, _, _ = struct.unpack('!HHHHHH', dns[:12])
    pos = 12; query = []
    for _ in range(questions):
        labels = []; cursor = pos; end = None; visited = set()
        while True:
            if cursor in visited: raise ValueError('DNS pointer loop')
            visited.add(cursor)
            n = dns[cursor]
            if n & 192 == 192:
                if end is None: end = cursor + 2
                cursor = ((n & 63) << 8) | dns[cursor+1]; continue
            cursor += 1
            if not n: break
            labels.append(dns[cursor:cursor+n].decode('ascii')); cursor += n
        pos = end if end is not None else cursor
        kind, cls = struct.unpack('!HH', dns[pos:pos+4]); pos += 4
        query.append({'name': '.'.join(labels), 'type': kind, 'class': cls})
    pseudo = ip[12:20] + struct.pack('!BBH', 0, 17, length)
    return {'time_ns': int(timestamp), 'direction': direction,
            'source': str(ipaddress.ip_address(ip[12:16])), 'destination': str(ipaddress.ip_address(ip[16:20])),
            'source_port': source, 'destination_port': dest, 'id': ident, 'response': bool(flags & 32768),
            'rcode': flags & 15, 'answers': answers, 'truncated': bool(flags & 512), 'queries': query,
            'ip_checksum_valid': checksum(ip[:ihl]) == 0,
            'udp_checksum_valid': check == 0 or checksum(pseudo + udp) == 0}

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('log', type=pathlib.Path); a = p.parse_args()
    rows = [decode(line) for line in a.log.read_text().splitlines() if line.startswith('HARDDIND_DNS ')]
    print(json.dumps({'kind': 'dns_link_packet_diagnostic', 'packets': rows}, indent=2))
