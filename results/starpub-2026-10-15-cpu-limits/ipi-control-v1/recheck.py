"""Reparse frozen IPI control evidence; successful execution is not full CPU acceptance."""
import gzip
import hashlib
import importlib.util
import json
import pathlib

root = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('frozen_checks', root / 'sources/scripts/cpu_limit_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
report = json.loads((root / 'runtime/report.json').read_text())
summaries = {}
for name, phase in report['phases'].items():
    container = 'cpulim-update' if name == 'live_update' else phase['spec']['name']
    text = gzip.decompress((root / 'runtime' / ('logs-' + container + '.stdout.gz')).read_bytes()).decode()
    events = checks.parse_events(text)
    assert events == phase['events'], name
    summaries[name] = checks.spin_summary(events)

reference = summaries['unthrottled_reference']
verdicts = {'unthrottled_reference': checks.classify_reference(reference, 4)}
for name in ('cpus_flag_quota', 'explicit_period_quota', 'cpuset_single', 'cpuset_range', 'cpuset_with_quota'):
    phase = report['phases'][name]['spec']
    summary = summaries[name]
    if phase['kind'] == 'quota':
        result = checks.classify_quota(summary, phase['cores'], phase['period_s'], reference)
    elif phase['kind'] == 'cpuset':
        result = checks.classify_cpuset(summary, phase['cpus'])
    else:
        result = checks.classify_cpuset(summary, phase['cpus'], phase['cores'], phase['period_s'], reference)
    verdicts[name] = result
windows = checks.classify_update_run(summaries['live_update'], [(2.0, 100000), (0.5, 100000), (2.0, 100000)], reference)
verdicts.update(zip(('update_window_initial_2', 'update_window_half', 'update_window_recovered_2'), windows))
for name, value in verdicts.items():
    assert value == report['checks'][name], name

# This is a synthetic counterexample against the actual frozen checker. It
# proves a classification defect, not a real quota violation or runtime pass.
a = dict(wall=0, cap_end=20_000_000, proc_ns=0, usage_usec=0,
         nr_throttled=0, throttled_usec=0, steal_ns=None)
b = dict(wall=1_020_000_000, cap_end=1_040_000_000, proc_ns=635_000_000,
         usage_usec=635000, nr_throttled=8, throttled_usec=500000, steal_ns=None)
counterexample = checks._window_result(0, .5, 100000, a, b, None, {'used': 2}, {})
assert counterexample['status'] == 'fail'
assert .635 / 1.04 < checks.cfs_band(.5, .1, 1.04)[1]
assert .635 > checks.cfs_band(.5, .1, 1.0)[1]

cleanup = json.loads((root / 'cleanup-audit.json').read_text())
assert cleanup['all_empty'] and len(cleanup['groups']) == 69
assert all(c['cleanup']['clean_shutdown'] and not c['timed_out'] for c in report['commands'])
assert report['final_state']['phase'] == 'stopped' and not report['final_state']['forced_shutdown']
assert report['checks']['inputs_unchanged']['status'] == 'pass'
assert report['full_acceptance'] is False and report['performance_acceptance'] is False
for remote, digest in report['sha256'].items():
    if '/repo/' in remote:
        path = root / 'sources' / remote.split('/repo/', 1)[1]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path

audit = {
    'scope': 'frozen diagnostic evidence integrity; not full feature acceptance',
    'reparsed_phase_verdicts': {k: v['status'] for k, v in verdicts.items()},
    'raw_commands': len(report['commands']),
    'process_groups_empty': len(cleanup['groups']),
    'runtime_verdict': report['verdict'],
    'checker_uncertainty_counterexample': {
        'scope': 'synthetic fixture', 'expected': 'indeterminate', 'actual': counterexample,
        'least_rate': .635 / 1.04, 'wide_upper': checks.cfs_band(.5, .1, 1.04)[1],
        'most_rate': .635, 'narrow_upper': checks.cfs_band(.5, .1, 1.0)[1],
    },
    'file_sha256': {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*')
        if p.is_file() and p.name != 'archive-audit.json' and '__pycache__' not in p.parts
    },
}
(root / 'archive-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
print(json.dumps({k: v for k, v in audit.items() if k not in ('file_sha256', 'checker_uncertainty_counterexample')}, indent=2))
