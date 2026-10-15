#!/usr/bin/env python3
"""Reparse the frozen seven cells; no runtime or performance acceptance."""
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'harness'))
from perf_log import parse_perf_text, timed_records


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(
        p.with_name(p.name + '.gz').read_bytes())


for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
status = json.loads(read('runtime/perf-status.json'))
summary = json.loads(read('runtime/perf-summary.json'))
assert owner['status'] == status['status'] == 'finished'
assert owner['exit_code'] == status['exit_code'] == 0
assert owner['harness_before'] == owner['harness_after']
assert owner['owner_groups_empty'] and status['groups_empty'] and status['inputs_unchanged']
assert not owner['full_acceptance'] and not owner['performance_acceptance']
assert all(x == [[], []] for x in owner['owner_groups'].values())
groups = json.loads(read('runtime/perf-cleanup-audit.json'))
assert len(groups) == 75 and all(x == [[], []] for x in groups.values())
for name, digest in status['frozen']['sources'].items():
    assert hashlib.sha256(read('sources/' + name)).hexdigest() == digest
for name, digest in owner['harness_before'].items():
    assert hashlib.sha256(read('harness/' + name)).hexdigest() == digest
expected_order = ['native-pre', 'eager-a', 'lazy1-a', 'lazy1-b',
                  'eager-b', 'lazy0-control', 'native-post']
assert list(status['cells']) == summary['cell_order'] == expected_order
stats = {}
for name, cell in status['cells'].items():
    assert cell['exit_code'] == 0 and cell['clean_shutdown']
    assert not cell['errors'] and not cell['timed_out']
    runs, errors = parse_perf_text(read(f'runtime/{name}.stdout').decode(),
                                  status['manifest']['binaries'])
    assert not errors and len(runs) == 35, (name, errors)
    timed = timed_records(runs)
    assert len(timed) == 25
    stats[name] = {}
    for workload, saved in cell['stats'].items():
        values = [r['json']['seconds'] for r in timed
                  if r['binary'] + '/' + r['workload'] == workload]
        median = statistics.median(values)
        computed = dict(n=len(values), median=median,
                        mad=statistics.median(abs(v - median) for v in values),
                        min=min(values), raw_seconds=values)
        assert computed == saved, (name, workload)
        stats[name][workload] = median
assert summary['descriptive_only'] and summary['no_threshold_or_verdict']
assert not summary['ratios']['excluded_cells']
for category, workloads in summary['ratios'].items():
    if category == 'excluded_cells':
        continue
    for workload, ratios in workloads.items():
        for label, ratio in ratios.items():
            if category in ('lazy1_over_eager', 'lazy0control_over_eager'):
                numerator, denominator = label.split('/')
            else:
                numerator = label
                denominator = 'native-pre' if category.endswith('_pre') else 'native-post'
            assert round(stats[numerator][workload] / stats[denominator][workload], 6) == ratio
events = [json.loads(line) for line in read('runtime/perf-events.jsonl').splitlines()]
start = next(e['ts'] for e in events if e['kind'] == 'stage' and e['name'] == 'native-post')
acquire = next(e['ts'] for e in events if e['kind'] == 'lock_acquired' and e['ts'] > start)
print(f'7 cells, 245 records (175 timed), medians/MAD/ratios replay identically; 75 + 2 groups empty.')
print(f'Native-post waited {acquire - start:.3f}s for lock before measurement. No acceptance verdict.')
