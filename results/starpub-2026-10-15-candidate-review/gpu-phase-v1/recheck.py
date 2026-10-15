#!/usr/bin/env python3
"""Rebuild phase medians from every raw sample, with controls kept separate."""
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
assert len(r['groups']) == 19 and all(v == [[], []] for v in r['groups'].values())
assert len(r['commands']) == 16 and not r['full_acceptance'] and not r['performance_acceptance']
assert r['ready_state']['config']['gpu_async_wait']
assert r['ready_state']['config']['kernel_sha256'] == 'b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502'
assert hashlib.sha256(read('repo/src/cuda_phase_probe.c')).hexdigest() == r['probe_source_sha256']
for cmd in r['commands']:
    assert cmd['exit_code'] == 0 and not cmd.get('timeout') and cmd['cleanup']['clean_shutdown']
fields = ['htod_a_ns', 'htod_b_ns', 'launch_ns', 'sync_ns', 'dtoh_ns', 'total_ns']
summary = {}
for phase in ('native-before', 'docker', 'native-after'):
    samples = r['samples'][phase]
    assert [s['order'] for s in samples] == ['forward', 'reverse', 'reverse', 'forward']
    for seq, sample in enumerate(samples):
        rows = [json.loads(x) for x in read(f'{phase}-{seq}.stdout').splitlines()]
        assert rows == sample['gpu_results'] and len(rows) == 32
        assert not read(f'{phase}-{seq}.stderr')
        order = sample['order']
        expected = [(gpu, it) for gpu in (range(4) if order == 'forward' else range(3, -1, -1)) for it in range(8)]
        assert [(x['gpu'], x['iteration']) for x in rows] == expected
        for x in rows:
            assert x['position'] == (x['gpu'] if order == 'forward' else 3 - x['gpu'])
            assert x['order'] == order and x['warmup'] == (x['iteration'] < 2)
            assert x['verified'] is True and x['elements'] == 1048576
            assert all(type(x[f]) is int and x[f] >= 0 for f in fields)
            assert sum(x[f] for f in fields[:-1]) == x['total_ns']
    summary[phase] = {}
    for order in ('forward', 'reverse'):
        summary[phase][order] = {}
        for gpu in range(4):
            rows = [x for sample in samples if sample['order'] == order for x in sample['gpu_results'] if x['gpu'] == gpu and not x['warmup']]
            assert len(rows) == 12
            summary[phase][order][str(gpu)] = {f: {'median_ns': statistics.median(x[f] for x in rows), 'raw_ns': [x[f] for x in rows]} for f in fields}
assert summary == r['summary']
print('PASS: all 384 phase records verified; integer timing sums, ABBA order, raw medians and 19 empty groups.')
print('Output memory is prefaulted by sentinel initialization; do not compare this directly to the old cold-buffer probe.')
