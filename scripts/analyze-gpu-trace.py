#!/usr/bin/env python3
"""Inventory device calls in a matching, complete outer CUDA trace; not GPU acceptance."""
import argparse
import collections
import gzip
import hashlib
import json
import pathlib
import re


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report', type=pathlib.Path)
    p.add_argument('trace', type=pathlib.Path)
    args = p.parse_args()
    report = json.loads(args.report.read_text())
    data = args.trace.read_bytes()
    if args.trace.suffix == '.gz':
        data = gzip.decompress(data)
    if (not report.get('diagnostic_complete') or report.get('internal_gpu_verified') is not False
            or hashlib.sha256(data).hexdigest() != report['trace_sha256']):
        raise ValueError('incomplete diagnostic or mismatched trace')
    counts = collections.Counter()
    mappings = collections.Counter()
    malformed = []
    pending = {}
    # strace -yy uses nested angle brackets: 8</dev/nvidiactl<char 195:255>>.
    device = r'<(/dev/nvidia[^<>,]+)(?:<[^>]*>)?>'
    for lineno, line in enumerate(data.decode().splitlines(), 1):
        start = re.match(r'(\d+) +\d+:\d+:\d+\.\d+ (ioctl|mmap)\(', line)
        resumed = re.match(r'(\d+) +\d+:\d+:\d+\.\d+ <\.\.\. (ioctl|mmap) resumed>', line)
        if start and '<unfinished ...>' in line:
            if start[1] in pending:
                raise ValueError('overlapping syscalls on one thread')
            pending[start[1]] = (lineno, start[2], line.split('<unfinished ...>', 1)[0])
            continue
        if resumed:
            original = pending.pop(resumed[1], None)
            if original is None or original[1] != resumed[2]:
                raise ValueError('resumed syscall without matching entry')
            lineno, _, prefix = original
            line = prefix + line[resumed.end():]
        entry = re.search(r'ioctl\(\d+' + device + r', ', line)
        if entry:
            tail = line[entry.end():]
            depth = 0
            for i, char in enumerate(tail):
                if char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                elif char == ',' and depth == 0:
                    counts[(entry[1], tail[:i])] += 1
                    break
            else:
                malformed.append(lineno)
        if re.search(r'\bmmap\(', line):
            mappings.update(re.findall(device, line))
    if pending or malformed or sum(counts.values()) != report['nvidia_ioctl_entries']:
        raise ValueError(f'unparsed NVIDIA ioctl entries: {malformed}')
    result = {'kind': 'outer_gpu_ioctl_inventory', 'full_acceptance': False,
              'trace_sha256': report['trace_sha256'], 'unparsed_entry_lines': malformed,
              'ioctl_entry_counts': [{'device': device, 'request': request, 'count': count}
                                     for (device, request), count in sorted(counts.items())],
              'device_mmap_entry_counts': dict(mappings),
              'limitations': ['counts include failed calls; no nested ioctl argument decoding',
                              'resumed lines are not counted as new syscall entries',
                              'outer trace only, not an implemented guest backend']}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
