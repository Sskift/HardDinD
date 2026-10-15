#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 1
assert owner['normal_shutdown'] and owner['inputs_unchanged'] and owner['groups_empty']
assert len(owner['groups']) == 14 and all(x == [[], []] for x in owner['groups'].values())
assert len(owner['commands']) == 11
assert set(owner['samples']) == {'native-before', 'docker'} and owner['samples']['docker'] == []
assert 'ratios' not in owner and 'summary' not in owner
for row in owner['commands']:
    assert row['exit_code'] == (1 if row['label'] == 'docker-0' else 0)
    assert not row.get('timeout') and row['cleanup']['clean_shutdown']
for sample in owner['samples']['native-before']:
    seq = sample['seq']
    assert sample['warmup'] == (seq < 2)
    raw = [json.loads(line) for line in read(f'native-before-{seq}.stdout').splitlines()]
    assert raw == sample['gpu_results'] and {x['gpu'] for x in raw} == set(range(4))
    assert all(x['verified'] is True and x['elements'] == 1048576 for x in raw)
assert not read('docker-0.stdout')
assert read('docker-0.stderr') == b'cuCtxCreate_v2(&ctx,0,dev): CUDA error 304\n'
case = next(row for row in owner['commands'] if row['label'] == 'docker-0')
assert not any('LD_PRELOAD' in arg or 'strace' in arg for arg in case['command'])
assert owner['ready_state']['config']['kernel_sha256'] == '9306f4e6045bb26821338c6962f8c67f887ea6f86f917524ebe4b22a25a25927'
print('Native 7 x 4 GPU results verified; first uninstrumented Docker run fails CUDA 304.')
print('No internal timing samples or native-after; 14 groups empty, normal stop. No performance verdict.')
