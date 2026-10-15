#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(
        p.with_name(p.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 1
assert not owner['normal_shutdown'] and owner['groups_empty'] and owner['inputs_unchanged']
assert not owner['full_acceptance'] and not owner['performance_acceptance']
assert len(owner['groups']) == 27 and all(x == [[], []] for x in owner['groups'].values())
assert 'native-after' not in owner
assert owner['native-before']['all_four_verified']
native = [json.loads(line) for line in read('native-before.stdout').splitlines()]
assert native == owner['native-before']['compute']
assert {row['gpu'] for row in native} == set(range(4))
assert all(row['verified'] is True and row['elements'] == 1048576 for row in native)
for i, case in enumerate(owner['cases']):
    label = f"case-{i}-{case['mode']}"
    assert case['mode'] == owner['sequence'][i]
    command = next(row for row in owner['commands'] if row['label'] == label)
    rows = [json.loads(line) for line in read(label + '.stdout').splitlines()]
    assert rows == case['compute'] and not case['timed_out']
    assert command['exit_code'] == case['exit_code'] == (0 if i < 6 else 1)
    assert case['all_four_verified'] == (i < 6)
    if i < 6:
        assert len(rows) == 4 and {r['gpu'] for r in rows} == set(range(4))
        assert all(r['verified'] is True and r['elements'] == 1048576 for r in rows)
    else:
        assert not rows
        assert b'cuCtxCreate_v2(&ctx,0,dev): CUDA error 304' in read(label + '.stderr')
    if case['mode'] in ('strace', 'both'):
        assert b'ioctl(' in read(label + '.strace')
failure = read('case-6-preload.stderr').decode().splitlines()
negative = [line for line in failure if re.search(r' rc=-\d+', line)]
assert len(negative) == 1
assert 'request=0xc030462b' in negative[0] and ' rc=-1 errno=5' in negative[0]
header = bytes.fromhex(re.search(r'before=([0-9a-f]+)', negative[0])[1])
assert int.from_bytes(header[12:16], 'little') == 0xc56f
assert owner['final_state']['shutdown_error'] == 'AF_UNIX path too long'
assert owner['final_state']['forced_shutdown'] and owner['final_state']['guest_exit_code'] == -9
assert len(owner['ready_state']['mconsole'].encode()) == 109
print('Four-card compute 6/8, two CUDA304 failures; failed preload RM_ALLOC classc56f returns EIO.')
print('Stop failed on 109-byte socket path; forced cleanup, 27 groups empty, no native-after.')
