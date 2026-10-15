#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 0
assert r['normal_shutdown'] and r['groups_empty'] and r['inputs_unchanged']
assert len(r['groups']) == 16 and all(v == [[], []] for v in r['groups'].values())
assert len(r['commands']) == 13 and not r['full_acceptance'] and not r['performance_acceptance']
assert hashlib.sha256(read('repo/src/cpu_touch_probe.c')).hexdigest() == r['probe_source_sha256']
fields = ['cold_ns', 'warm_ns', 'cold_minor', 'warm_minor', 'cold_major', 'warm_major']
summary = {}
for phase in ('native-before', 'docker', 'native-after'):
    samples = r['samples'][phase]
    assert len(samples) == 3
    for seq, sample in enumerate(samples):
        records = [json.loads(x) for x in read(f'{phase}-{seq}.stdout').splitlines()]
        assert records == sample['records'] and len(records) == 24
        assert not read(f'{phase}-{seq}.stderr')
        assert [(x['bytes'], x['iteration']) for x in records] == [(size, it) for size in (1048576, 4194304, 16777216) for it in range(8)]
        for x in records:
            assert x['verified'] is True and x['page_size'] == 4096
            assert x['pages'] * x['page_size'] == x['bytes']
            assert all(type(x[f]) is int and x[f] >= 0 for f in fields)
            assert x['cold_minor'] - x['pages'] in ((0,) if phase == 'docker' else (0, 1))
            assert x['warm_minor'] == x['cold_major'] == x['warm_major'] == 0
    summary[phase] = {str(size): {f: {
        'median': statistics.median(x[f] for sample in samples for x in sample['records'] if x['bytes'] == size),
        'raw': [x[f] for sample in samples for x in sample['records'] if x['bytes'] == size]
        } for f in fields} for size in (1048576, 4194304, 16777216)}
assert summary == r['summary']
for cmd in r['commands']:
    assert cmd['exit_code'] == 0 and not cmd.get('timeout') and cmd['cleanup']['clean_shutdown']
    if cmd['label'].startswith('docker-'):
        assert not any(arg.startswith('--device=') for arg in cmd['command'])
print('PASS: 216 CPU-only cold/warm records, fault counts and independent native brackets rechecked; 16 empty groups.')
print('Anonymous first-touch overhead is observed independently of CUDA; no performance acceptance threshold applied.')
