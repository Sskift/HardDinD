#!/usr/bin/env python3
"""Verify archived witness observations; does not assign a cause or pass performance."""
import gzip
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def load(name):
    return json.loads(read(name))


def check(value, message):
    if not value:
        raise SystemExit(message)


def groups(value):
    result = set()
    if isinstance(value, dict):
        if 'process_group' in value and 'clean_shutdown' in value:
            check(value['clean_shutdown'] and not value['forced_cleanup'] and
                  not value['remaining_live_pids'] and not value['remaining_zombie_pids'],
                  'unclean recorded process group')
            result.add(str(value['process_group']))
        for child in value.values():
            result.update(groups(child))
    elif isinstance(value, list):
        for child in value:
            result.update(groups(child))
    return result


for name, digest in load('archive-sha256.json').items():
    check(hashlib.sha256(read(name)).hexdigest() == digest, 'archive changed: ' + name)
owner = load('owner-status.json')
check(owner['status'] == 'finished' and owner['exit_code'] == 0 and
      owner['inputs_unchanged'] and owner['groups_empty'] and owner['manifest_verified'] and
      not owner['full_acceptance'] and not owner['performance_acceptance'], 'owner boundary changed')
for name, digest in owner['sources_before'].items():
    check(hashlib.sha256(read(name)).hexdigest() == digest, 'source changed: ' + name)
report = load('runtime/witness-v3/report.json')
check(report['status'] == 'finished' and report['exit_code'] == 0 and
      report['normal_shutdown'] and report['inputs_unchanged'] and
      report['witness_binary_unchanged'] and not report['full_acceptance'] and
      not report['performance_acceptance'], 'runtime incomplete')
analysis = load('owner-analysis.json')
check(analysis['status'] == 'finished' and analysis['exit_code'] == 0 and
      not analysis['full_acceptance'] and not analysis['performance_acceptance'], 'analysis failed')
cells = {'quiet-quota', 'quiet-unlimited', 'separate-observer', 'same-observer'}
check(set(report['cells']) == set(analysis['cells']) == cells, 'missing or extra cells')
for name in sorted(cells):
    cell = report['cells'][name]
    derived = analysis['cells'][name]
    prefix = 'runtime/witness-v3/' + name + '/'
    rows = [json.loads(line) for line in read(prefix + 'witness.stdout').splitlines()]
    caps = [row for row in rows if row['event'] == 'cap']
    summaries = [json.loads(line) for line in read(prefix + 'witness.stderr').splitlines()]
    correlation = load(prefix + 'corr/correlation.json')
    check(cell['status'] == 'complete' and cell['failure'] is None and
          [row['k'] for row in caps] == list(range(60)) and
          sorted(row['tag'] for row in rows if row['event'] == 'sync') == ['end', 'start'] and
          len(summaries) == 1 and summaries[0]['requested'] == summaries[0]['completed'] == 60 and
          summaries[0]['status'] == 'complete', 'incomplete witness: ' + name)
    check(correlation['attribution_valid'] and correlation['coverage_counts'] == {'covered': 300} and
          derived['raw_correlation_replay_identical'] and
          derived['source_sha256'] == hashlib.sha256(read('diag/host_correlate.py')).hexdigest(),
          'correlation evidence differs: ' + name)
    durations = [row['stages'][-1]['end_ns'] - row['mono_begin_ns'] for row in caps]
    gaps = [v['mono_begin_ns'] - u['mono_begin_ns'] for u, v in zip(caps, caps[1:])]
    for key, values in [('capture', durations), ('inter_capture', gaps)]:
        expected = {'count': len(values), 'median_ms': statistics.median(values) / 1e6,
                    'p95_ms': sorted(values)[int((len(values) - 1) * .95)] / 1e6,
                    'max_ms': max(values) / 1e6}
        check(derived[key] == expected, 'raw latency reparse differs: ' + name)
    check(derived['longest_stage'] == max(correlation['stages'], key=lambda row: row['guest_wall_ns']),
          'longest stage differs')
    sampler = cell['sampler']
    check(sampler['rc'] == 0 and not sampler['forced_kill'] and not sampler['timed_out'] and
          sampler['termination_reason'] == 'signal' and sampler['cpu_s'] > 0 and
          sampler == derived['sampler'], 'sampler evidence incomplete')
    if name in ('separate-observer', 'same-observer'):
        busy = cell['busy_load']
        check(busy['before']['running'] and busy['after_alive']['running'] and
              busy['before']['started_at'] == busy['after_alive']['started_at'] and
              not busy['final']['running'] and busy['final']['exit_code'] == '0',
              'busy load did not cover observer cell')
    print(name, 'max capture ms:', round(max(durations) / 1e6, 3),
          'sampler CPU s:', sampler['cpu_s'], 'elapsed s:', round(sampler['elapsed_s'], 3))
audit = load('owner-cleanup-audit.json')
check(len(audit) == 39 and set(audit) == groups(owner) | groups(report) and
      all(value == [[], []] for value in audit.values()), 'cleanup audit incomplete')
print('Four complete diagnostic cells, raw latency summaries and 39 empty groups verified.')
print('Sampling overhead is material; no performance acceptance or proven kernel cause.')
