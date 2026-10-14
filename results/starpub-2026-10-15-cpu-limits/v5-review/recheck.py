"""Re-evaluate immutable raw logs with the integrated checker, preserving originals."""
import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

archive = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('checks', archive / 'sources/scripts/cpu_limit_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
def reparse(root):
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
        assert value['status'] == report['checks'][name]['status'], name
    
    return verdicts

results = {name: reparse(archive.parent / name)
           for name in ('v4', 'ipi-control-v1', 'ipi-control-v2')}

# The original false-FAIL example now stays indeterminate; definite violation
# and compliance on the same capture geometry must remain distinct.
a = dict(wall=0, cap_end=20_000_000, proc_ns=0, usage_usec=0,
         nr_throttled=0, throttled_usec=0, steal_ns=None)
synthetic = {}
for delta, expected in ((.635, 'indeterminate'), (.7, 'fail'), (.6, 'pass')):
    b = dict(wall=1_020_000_000, cap_end=1_040_000_000,
             proc_ns=round(delta * 1e9), usage_usec=round(delta * 1e6),
             nr_throttled=8, throttled_usec=500000, steal_ns=None)
    result = checks._window_result(0, .5, 100000, a, b, None, {'used': 2}, {})
    assert result['status'] == expected
    synthetic[str(delta)] = result

report = json.loads((archive.parent / 'ipi-control-v2/runtime/report.json').read_text())
for idx in (0, -1):
    events = copy.deepcopy(report['phases']['cpus_flag_quota']['events'])
    del events[idx]['cap_end_ns']
    try:
        checks.spin_summary(events)
    except ValueError:
        pass
    else:
        raise AssertionError('missing capture boundary accepted')
samples = [copy.deepcopy(e) for e in report['phases']['live_update']['events'] if e['event'] == 'sample']
samples[0]['q2_quota'] = 99999
assert checks.stable_timeline(samples, [(2, 100000), (.5, 100000), (2, 100000)])[0] == 'fail'
manifest = json.loads((archive / 'manifest.json').read_text())
for path, digest in manifest.items():
    assert hashlib.sha256((archive / 'sources' / path).read_bytes()).hexdigest() == digest
assert json.loads((archive / 'validation-result.json').read_text())['exit_code'] == 0
out = {'scope': 'offline reanalysis and measurement repair; no new runtime/performance acceptance',
       'reparsed_results': results, 'synthetic_boundary_results': synthetic,
       'missing_boundary_rejected': True, 'contradictory_stability_rejected': True,
       'file_sha256': {str(p.relative_to(archive)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in archive.rglob('*') if p.is_file()
                       and p.name != 'reanalysis.json' and '__pycache__' not in p.parts}}
(archive / 'reanalysis.json').write_text(json.dumps(out, indent=2) + '\n')
print(json.dumps({name: {phase: v['status'] for phase, v in data.items()}
                  for name, data in results.items()}, indent=2))
