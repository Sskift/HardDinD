#!/usr/bin/env python3
"""Summarize complete virtiofs diagnostic logs, without claiming performance parity."""
import argparse
import hashlib
import json
import pathlib
import re


def read_profile(path):
    rows = []
    ends = []
    seen = set()
    fields = {'opcode', 'lock_type', 'count', 'attempts', 'queue_ns', 'service_ns',
              'reply_ns', 'total_ns', 'max_total_ns'}
    for line in path.read_text().splitlines():
        marker = 'HARDDIND_REQUEST_PROFILE '
        if marker in line:
            row = json.loads(line.split(marker, 1)[1])
            if set(row) != fields or any(type(v) is not int or v < 0 for v in row.values()):
                raise ValueError('invalid diagnostic counters')
            key = (row['opcode'], row['lock_type'])
            if key in seen or not row['count'] or row['attempts'] < row['count']:
                raise ValueError('duplicate or incomplete diagnostic row')
            seen.add(key)
            if (sum(row[k] for k in ('queue_ns', 'service_ns', 'reply_ns')) > row['total_ns']
                    or not row['max_total_ns'] <= row['total_ns'] <= row['max_total_ns'] * row['count']):
                raise ValueError('inconsistent diagnostic durations')
            rows.append(row)
        if 'HARDDIND_REQUEST_PROFILE_END' in line:
            match = re.search(r'HARDDIND_REQUEST_PROFILE_END rows=(\d+)$', line)
            if not match:
                raise ValueError('invalid completion marker')
            ends.append(int(match[1]))
    if ends != [len(rows)] or not rows:
        raise ValueError('missing, duplicate, or inconsistent profile completion')
    for row in rows:
        row['operation'] = {32: 'SETLK', 33: 'SETLKW'}.get(row['opcode'], str(row['opcode']))
        row['average_us'] = {name[:-3]: row[name] / row['count'] / 1000
                             for name in ('queue_ns', 'service_ns', 'reply_ns', 'total_ns')}
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'rows': rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('logs', nargs='+', type=pathlib.Path)
    args = p.parse_args()
    result = {'kind': 'virtiofs_request_diagnostic', 'full_acceptance': False,
              'limitations': [
                  'instrumentation changes scheduling and adds overhead',
                  'timestamps begin after descriptor decoding; exclude guest and transport latency',
                  'service includes server logic, not just the host syscall',
                  'interrupt control messages are excluded',
                  'retry time after the first attempt is included in total, not initial queue time',
                  'profile aggregation and logging overhead are outside the measured intervals'],
              'profiles': [read_profile(path) for path in args.logs]}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
