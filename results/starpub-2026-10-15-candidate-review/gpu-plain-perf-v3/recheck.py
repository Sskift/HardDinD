#!/usr/bin/env python3
"""Recompute descriptive GPU timings from uninstrumented raw outputs."""
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
assert len(r['groups']) == 27 and all(value == [[], []] for value in r['groups'].values())
assert len(r['commands']) == 24
assert r['descriptive_only'] and not r['performance_acceptance'] and not r['full_acceptance']
assert r['warmups'] == 2 and r['timed'] == 5 and r['host_affinity'] == [0, 1, 2, 3]
config = r['ready_state']['config']
assert config['kernel_sha256'] == 'b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502'
assert config['gpu_async_wait'] and config['memfd_ram'] and config['memfd_stub']
assert config['cpus'] == 4
for row in r['commands']:
    assert row['exit_code'] == 0 and not row.get('timeout') and row['cleanup']['clean_shutdown']
    assert not any('strace' in arg or 'LD_PRELOAD' in arg for arg in row['command'])
summary = {}
for phase in ('native-before', 'docker', 'native-after'):
    samples = r['samples'][phase]
    assert len(samples) == 7
    for seq, sample in enumerate(samples):
        assert sample['seq'] == seq and sample['warmup'] == (seq < 2)
        records = [json.loads(x) for x in read(f'{phase}-{seq}.stdout').splitlines()]
        assert records == sample['gpu_results'] and len(records) == 4
        assert {x['gpu'] for x in records} == set(range(4))
        assert all(x['verified'] is True and x['elements'] == 1048576 and x['seconds'] > 0 for x in records)
        assert not read(f'{phase}-{seq}.stderr')
    summary[phase] = {}
    for gpu in range(4):
        values = [next(x['seconds'] for x in sample['gpu_results'] if x['gpu'] == gpu)
                  for sample in samples if not sample['warmup']]
        median = statistics.median(values)
        summary[phase][str(gpu)] = {'median_seconds': median, 'raw_seconds': values,
                                  'mad_seconds': statistics.median(abs(x - median) for x in values)}
assert summary == r['summary']
ratios = {phase: {str(gpu): summary['docker'][str(gpu)]['median_seconds'] /
                 summary[phase][str(gpu)]['median_seconds'] for gpu in range(4)}
          for phase in ('native-before', 'native-after')}
assert ratios == r['ratios']
print('PASS: 21 processes x 4 GPU results verified, async ON, no tracing; 27 empty groups and normal stop.')
print('Descriptive ratios only, separate native brackets:', json.dumps(ratios, sort_keys=True))
