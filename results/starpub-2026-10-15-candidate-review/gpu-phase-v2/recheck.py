#!/usr/bin/env python3
"""Recheck cold-output and prefault controls, keeping GPU order separate."""
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
    assert [s['order'] for s in samples] == ['forward', 'forward', 'reverse', 'reverse']
    for seq, sample in enumerate(samples):
        rows = [json.loads(x) for x in read(f'{phase}-{seq}.stdout').splitlines()]
        assert rows == sample['gpu_results'] and len(rows) == 32
        assert not read(f'{phase}-{seq}.stderr')
        order = sample['order']
        mode = ['prefault', 'cold', 'cold', 'prefault'][seq]
        assert sample['output_mode'] == mode
        command = next(c['command'] for c in r['commands'] if c['label'] == f'{phase}-{seq}')
        assert command[-2:] == [order, mode]
        expected = [(gpu, it) for gpu in (range(4) if order == 'forward' else range(3, -1, -1)) for it in range(8)]
        assert [(x['gpu'], x['iteration']) for x in rows] == expected
        for x in rows:
            assert x['position'] == (x['gpu'] if order == 'forward' else 3 - x['gpu'])
            assert x['order'] == order and x['warmup'] == (x['iteration'] < 2)
            assert x['verified'] is True and x['elements'] == 1048576
            assert all(type(x[f]) is int and x[f] >= 0 for f in fields)
            assert sum(x[f] for f in fields[:-1]) == x['total_ns']
    summary[phase] = {str(sample['seq']): {str(gpu): {f: {
        'median_ns': statistics.median(x[f] for x in sample['gpu_results'] if x['gpu'] == gpu and not x['warmup']),
        'first_ns': next(x[f] for x in sample['gpu_results'] if x['gpu'] == gpu and x['iteration'] == 0)
        } for f in fields} for gpu in range(4)} for sample in samples}
assert summary == r['summary_by_case']
print('PASS: all 384 computations, 4 cold/prefault+order cases in each bracket; source and 19 empty groups verified.')
print('First output touch cost follows the first GPU; this does not yet count page faults or remove application work.')
