#!/usr/bin/env python3
"""Reproduce the finalization failure without running workloads."""
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(path.with_name(path.name + '.gz').read_bytes())


for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 1
for name, digest in owner['sources_before'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest, name
assert not (ROOT / 'runtime/shares-v5/report.json').exists()
assert not (ROOT / 'runtime/shares-v5/report.json.gz').exists()
assert b"AttributeError: 'str' object has no attribute 'get'" in read('runtime.log')
assert b'Ran 100 tests' in read('checker-and-real-capture-tests.stderr')
values = []
for line in read('runtime/shares-v5/instance-start.stdout').decode().splitlines():
    try:
        values.append(json.loads(line))
    except json.JSONDecodeError:
        pass
assert values and isinstance(values[0], str)
try:
    [value.get('event') for value in values[:3]]
except AttributeError:
    pass
else:
    raise SystemExit('the real finalization failure was not reproduced')
assert json.loads(read('runtime/shares-v5/wait-a.stdout')) == 0
assert json.loads(read('runtime/shares-v5/wait-b.stdout')) == 0
audit = json.loads(read('owner-postfailure-audit.json'))
assert audit['source_hashes_unchanged'] and audit['normal_guest_stop']
assert len(audit['known_groups']) == 4 and all(v == [[], []] for v in audit['known_groups'].values())
assert audit['full_command_group_audit'].startswith('unavailable:')
assert not audit['full_acceptance'] and not audit['performance_acceptance']
print('Actual scalar-JSON report crash reproduced; only four recoverable groups audited.')
print('The incomplete run remains failed. Functional and performance acceptance are not claimed.')
