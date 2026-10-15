#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['groups_empty'] and owner['sources_unchanged'] and owner['libraries_unchanged']
for name, digest in owner['sources'].items():
    if name != 'cuda-probe':
        assert hashlib.sha256(read(name)).hexdigest() == digest
assert len(owner['commands']) == len(owner['groups']) == 2
assert all(x == [[], []] for x in owner['groups'].values())
for row in owner['commands']:
    assert row['exit_code'] == 0 and row['cleanup']['clean_shutdown'] and not row.get('timeout')
compute = [json.loads(line) for line in read('native-cuda.stdout').splitlines()]
assert compute == owner['compute'] and {x['gpu'] for x in compute} == {0, 1, 2, 3}
assert all(x['verified'] is True and x['elements'] == 1048576 for x in compute)
sizes = {'0x20808159': 332, '0x20810108': 992, '0x20808162': 1}
selected = []
for line in read('native-cuda.stderr').decode().splitlines():
    row = dict(re.findall(r'(\w+)=([^ ]*)', line))
    command = row.get('rm_command')
    if command not in sizes:
        continue
    expected = sizes[command]
    assert row['rc'] == '0' and int(row['params_size']) == int(row['params_limit']) == expected
    assert len(bytes.fromhex(row['params_before'])) == len(bytes.fromhex(row['params_after'])) == expected
    before, after = bytes.fromhex(row['before']), bytes.fromhex(row['after'])
    assert len(before) == len(after) == 32
    assert int.from_bytes(after[28:32], 'little') == 0
    if command == '0x20808159':
        data = bytes.fromhex(row['params_before'])
        assert data == bytes.fromhex(row['params_after'])
        words = [int.from_bytes(data[i:i+4], 'little') for i in range(0, len(data), 4)]
        assert words == [2, 12, 0, 37] + [0] * 79
    selected.append(row)
assert selected == owner['full_captures']
assert {cmd: sum(x['rm_command'] == cmd for x in selected) for cmd in sizes} == owner['counts'] == dict.fromkeys(sizes, 4)
print('12 complete native control captures and four native CUDA computations verified; 2 groups empty.')
print('Private structure/GSP routing and internal Docker compute are not inferred.')
