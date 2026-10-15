#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 1
assert r['inputs_unchanged'] and r['groups_empty'] and len(r['groups']) == 9
assert all(value == [[], []] for value in r['groups'].values())
assert r['final_state'] == {} and not r['normal_shutdown']
assert set(r['samples']) == {'native-before'} and len(r['samples']['native-before']) == 7
assert 'summary' not in r and 'ratios' not in r
for sample in r['samples']['native-before']:
    rows = [json.loads(x) for x in read(f"native-before-{sample['seq']}.stdout").splitlines()]
    assert rows == sample['gpu_results'] and len(rows) == 4
    assert {x['gpu'] for x in rows} == set(range(4))
    assert all(x['verified'] is True and x['elements'] == 1048576 for x in rows)
log = read('instances/gpu-plain-v2/supervisor.log')
assert b'No such file or directory' in log and b'/run/network-backend' in log
assert [(c['label'], c['exit_code']) for c in r['commands']][-2:] == [('start', 1), ('stop', 1)]
print('PASS: setup failure preserved; no guest or internal timing result, native-before 7 x 4 checks only.')
