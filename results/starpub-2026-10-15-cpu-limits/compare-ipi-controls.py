"""Compare recorded flag controls; this is not a causal or performance test."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
names = ('v4', 'ipi-control-v1', 'ipi-control-v2')
reports = {name: json.loads((root / name / 'runtime/report.json').read_text())
           for name in names}
off, on = [json.loads((root / name / 'control-manifest.json').read_text())
           for name in names[1:]]
assert set(off['file_sha256']) == set(on['file_sha256'])
changed = [p for p in off['file_sha256']
           if off['file_sha256'][p] != on['file_sha256'][p]]
assert changed == ['scripts/harddind.py']
source = 'sources/scripts/harddind.py'
baseline = (root / 'v4' / source).read_text()
assert (root / names[2] / source).read_text() == baseline
assert (root / names[1] / source).read_text() == baseline.replace('defer_ipi=1', 'defer_ipi=0')
for name, manifest in zip(names[1:], (off, on)):
    for rel, digest in manifest['file_sha256'].items():
        p = root / name / 'sources' / rel
        if p.exists():
            assert hashlib.sha256(p.read_bytes()).hexdigest() == digest

comparison = {}
for name, flag in zip(names, (1, 0, 1)):
    report = reports[name]
    state = report['final_state']
    assert f'defer_ipi={flag}' in state['command']
    assert state['phase'] == 'stopped' and not state['forced_shutdown']
    assert report['checks']['inputs_unchanged']['status'] == 'pass'
    assert report['full_acceptance'] is False and report['performance_acceptance'] is False
    for identity in ('kernel_sha256', 'disk_sha256', 'network_sha256'):
        assert state['config'][identity] == reports['v4']['final_state']['config'][identity]
    phases = {}
    for phase, data in report['phases'].items():
        samples = [e for e in data['events'] if e['event'] == 'sample']
        phases[phase] = {
            'sample_count': len(samples),
            'max_capture_ms': max(e['cap_dur_ns'] for e in samples) / 1e6,
            'max_gap_after_capture_s': max(
                (b['wall_ns'] - a['cap_end_ns']) / 1e9
                for a, b in zip(samples, samples[1:])),
        }
    comparison[name] = {
        'defer_ipi': flag, 'original_verdict': report['verdict'],
        'phases': phases,
        'update_windows': {
            k: {'status': v['status'], 'evidence': v['evidence']}
            for k, v in report['checks'].items() if k.startswith('update_window_')
        },
    }
output = {
    'scope': 'sequential diagnostic flag controls; no causal or performance acceptance',
    'conclusion': 'both off and repeated on passed this subset; flag causality is unresolved',
    'results': comparison,
}
(root / 'ipi-control-comparison.json').write_text(json.dumps(output, indent=2) + '\n')
print(json.dumps({k: {'verdict': v['original_verdict'], 'live_update': v['phases']['live_update']}
                  for k, v in comparison.items()}, indent=2))
