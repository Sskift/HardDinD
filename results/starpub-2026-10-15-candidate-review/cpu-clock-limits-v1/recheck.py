#!/usr/bin/env python3
"""Replay the busy-load clock comparison; integrity is not acceptance."""
from pathlib import Path
import gzip, hashlib, importlib.util, json, statistics, sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent

def read(name):
    p = root / name
    return p.read_bytes() if p.is_file() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())

def obj(name):
    return json.loads(read(name))

archive = obj('archive-sha256.json')
for name, digest in archive.items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = obj('owner-status.json')
prepared = obj('prepare.json')
audit = obj('post-run-audit.json')
assert owner['status'] == 'finished' and owner['exit_code'] == 0
for flag in ['groups_empty', 'inputs_unchanged', 'external_inputs_unchanged']:
    assert owner[flag] and audit[flag], flag
assert all(v == [[], []] for v in owner['groups'].values())
assert all(v == [[], []] for v in audit['groups'].values())
assert not any(owner[k] for k in ['full_acceptance', 'performance_acceptance', 'resource_enforcement_acceptance'])
assert owner['inputs'] == prepared['inputs']
assert set(prepared['inputs']) - set(archive) == {'baseline-kernel', 'candidate-kernel'}
for name, digest in prepared['inputs'].items():
    if name in archive:
        assert hashlib.sha256(read(name)).hexdigest() == digest, name
assert read('baseline.config') == read('candidate.config')
prior = root.parent / 'cpu-clock-v1'
assert obj('clock-source-binding.json') == json.loads((prior / 'prepare.json').read_text())
for name in ['baseline-kernel', 'candidate-kernel', 'baseline.config', 'candidate.config']:
    assert prepared['inputs'][name] == obj('clock-source-binding.json')['files'][name]

spec = importlib.util.spec_from_file_location('frozen_cpu_checks', root / 'repo/scripts/cpu_limit_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
labels = ['baseline', 'off-before', 'on-first', 'on-second', 'off-after']
assert list(owner['cells']) == labels
stats = {}
commands = captures = 0
for label in labels:
    cell = owner['cells'][label]
    assert cell['completed'] and cell['normal_shutdown']
    name = 'clklim-' + label
    folder = 'runtime/' + name + '/'
    report = obj(folder + 'report.json')
    assert not report['full_acceptance'] and not report['performance_acceptance']
    assert not report['errors']
    assert report['verdict'] == cell['verdict']
    assert cell['runner_exit_code'] == {'pass': 0, 'fail': 1, 'indeterminate': 2}[report['verdict']]
    summaries = {}
    stats[label] = {'verdict': report['verdict'], 'phases': {}}
    for phase, evidence in report['phases'].items():
        container = 'cpulim-update' if phase == 'live_update' else evidence['spec']['name']
        events = checks.parse_events(read(folder + 'logs-' + container + '.stdout').decode())
        assert events == evidence['events']
        summaries[phase] = checks.spin_summary(events)
        samples = summaries[phase]['samples']
        durations = [(x['cap_end_ns'] - x['wall_ns']) / 1e6 for x in samples]
        gaps = [(v['wall_ns'] - u['wall_ns']) / 1e6 for u, v in zip(samples, samples[1:])]
        captures += len(samples)
        stats[label]['phases'][phase] = {
            'samples': len(samples), 'capture_median_ms': statistics.median(durations),
            'capture_max_ms': max(durations), 'inter_capture_max_ms': max(gaps),
            'rates': summaries[phase]['rates']}
        if phase != 'live_update':
            assert evidence['probe_exit'] == '0' and evidence['removed'] and not evidence['parse_error']
            assert evidence['created_id'] == evidence['inspect_start']['id'] == evidence['inspect_end']['id']
    reference = summaries['unthrottled_reference']
    verdicts = {'unthrottled_reference': checks.classify_reference(reference, 4)}
    for phase in ['cpus_flag_quota', 'explicit_period_quota', 'cpuset_single', 'cpuset_range', 'cpuset_with_quota']:
        s = report['phases'][phase]['spec']
        summary = summaries[phase]
        if s['kind'] == 'quota':
            v = checks.classify_quota(summary, s['cores'], s['period_s'], reference)
        elif s['kind'] == 'cpuset':
            v = checks.classify_cpuset(summary, s['cpus'])
        else:
            v = checks.classify_cpuset(summary, s['cpus'], s['cores'], s['period_s'], reference)
        verdicts[phase] = v
    schedule = [(2.0, 100000), (0.5, 100000), (2.0, 100000)]
    windows = checks.classify_update_run(summaries['live_update'], schedule, reference)
    verdicts.update(zip(['update_window_initial_2', 'update_window_half', 'update_window_recovered_2'], windows))
    for phase, verdict in verdicts.items():
        assert verdict == report['checks'][phase], (label, phase)
    timeline_status, timeline_evidence = checks.stable_timeline(summaries['live_update']['samples'], schedule)
    timeline = {'status': timeline_status, 'reason': timeline_evidence.pop('reason'), 'evidence': timeline_evidence}
    assert json.loads(json.dumps(timeline)) == report['checks']['update_timeline']
    update = report['phases']['live_update']
    assert len(set(update['identities'])) == 1 and update['identities'][0] == update['created_id']
    assert update['wait_output'] == '0' and not update['parse_error']
    st = report['final_state']
    assert st == cell['final_state']
    assert st['phase'] == 'stopped' and st['guest_exit_code'] == 0 and not st['forced_shutdown']
    mode = None if label == 'baseline' else '1' if label.startswith('on-') else '0'
    assert 'ncpus=4' in st['command'] and 'seccomp_threads=1' in st['command']
    assert [s for s in st['command'] if s.startswith('harddind_hr_sched_clock=')] == ([] if mode is None else ['harddind_hr_sched_clock=' + mode])
    assert all(c['cleanup']['clean_shutdown'] and not c['timed_out'] and c['exit_code'] == 0 for c in report['commands'])
    assert report['checks']['lifecycle_clean']['status'] == 'pass'
    assert report['checks']['inputs_unchanged']['status'] == 'pass'
    required = list(verdicts) + ['lifecycle_clean', 'inputs_unchanged', 'config_writes_recorded',
                                 'update_timeline', 'same_container_identity']
    statuses = [report['checks'][k]['status'] for k in required]
    aggregate = 'fail' if 'fail' in statuses else 'indeterminate' if any(s != 'pass' for s in statuses) else 'pass'
    assert aggregate == report['verdict']
    commands += len(report['commands'])
    for path, digest in report['sha256'].items():
        if '/repo/' in path:
            assert hashlib.sha256(read('repo/' + path.split('/repo/', 1)[1])).hexdigest() == digest
    stats[label]['nonpass'] = {k: v for k, v in report['checks'].items() if v['status'] != 'pass'}

for command in owner['commands']:
    assert not command.get('timeout') and command['cleanup']['clean_shutdown']
assert owner['commands'][0]['exit_code'] == 0
result = {'cells': stats, 'raw_command_count': commands, 'capture_count': captures,
          'process_groups_empty': len(audit['groups']), 'full_acceptance': False,
          'performance_acceptance': False, 'resource_enforcement_acceptance': False}
if sys.argv[1:] == ['--write-analysis']:
    with (root / 'owner-analysis.json').open('x') as out:
        out.write(json.dumps(result, indent=2) + '\n')
elif sys.argv[1:]:
    raise SystemExit('usage: recheck.py [--write-analysis]')
assert result == obj('owner-analysis.json')
print(f'PASS: five real Docker busy-load cells replayed; {commands} commands, {captures} captures, {len(audit["groups"])} empty groups.')
print('Runtime verdicts: ' + ', '.join(f'{k}={v["verdict"]}' for k, v in stats.items()))
print('No native Docker performance or complete resource-control acceptance.')
