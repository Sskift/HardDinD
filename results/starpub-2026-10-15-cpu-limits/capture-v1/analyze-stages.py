"""Audit the per-operation diagnostic; added observer cost is not a speed test."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parent
report = json.loads((root / 'runtime/report.json').read_text())
expected = ['cpu.max.first', 'cpu.stat', 'cpu.max.second', '/proc/stat', 'process_clock']
phases = {}
for name, phase in report['phases'].items():
    container = 'cpulim-update' if name == 'live_update' else phase['spec']['name']
    raw = gzip.decompress((root / 'runtime' / ('logs-' + container + '.stdout.gz')).read_bytes()).decode()
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert events == phase['events']
    rows = []
    peaks = {stage: {'operation_ms': 0, 'clock_before_ms': 0, 'clock_after_ms': 0,
                     'thread_cpu_ms': 0} for stage in expected}
    for index, sample in enumerate(e for e in events if e['event'] == 'sample'):
        assert sample['cap_dur_ns'] == sample['cap_end_ns'] - sample['wall_ns']
        assert [s['name'] for s in sample['stages']] == expected
        previous = sample['wall_ns']
        operations = []
        for stage in sample['stages']:
            assert previous <= stage['begin_ns'] <= stage['work_begin_ns'] <= stage['work_end_ns'] <= stage['end_ns'] <= sample['cap_end_ns']
            assert stage['thread_cpu_begin_ns'] <= stage['thread_cpu_end_ns']
            previous = stage['end_ns']
            timing = {
                'operation_ms': (stage['work_end_ns'] - stage['work_begin_ns']) / 1e6,
                'clock_before_ms': (stage['work_begin_ns'] - stage['begin_ns']) / 1e6,
                'clock_after_ms': (stage['end_ns'] - stage['work_end_ns']) / 1e6,
                'thread_cpu_ms': (stage['thread_cpu_end_ns'] - stage['thread_cpu_begin_ns']) / 1e6,
            }
            for key, value in timing.items():
                peaks[stage['name']][key] = max(peaks[stage['name']][key], value)
            operations.append({'name': stage['name'], **timing})
        rows.append({'index': index, 'capture_ms': sample['cap_dur_ns'] / 1e6,
                     'at_s': (sample['wall_ns'] - events[0]['wall_ns']) / 1e9,
                     'operations': operations})
    phases[name] = {'sample_count': len(rows), 'stage_peaks': peaks,
                    'slow_sample_count': sum(r['capture_ms'] > 25 for r in rows),
                    'longest_captures': sorted(rows, key=lambda r: r['capture_ms'], reverse=True)[:10]}
cleanup = json.loads((root / 'cleanup-audit.json').read_text())
assert cleanup['all_empty'] and len(cleanup['groups']) == 69
assert report['checks']['inputs_unchanged']['status'] == 'pass'
assert report['final_state']['phase'] == 'stopped' and not report['final_state']['forced_shutdown']
for remote, digest in report['sha256'].items():
    if '/repo/' in remote:
        p = root / 'sources' / remote.split('/repo/', 1)[1]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest
output = {'scope': 'five-operation timing with additional observer overhead; no performance acceptance',
          'runtime_verdict': report['verdict'], 'phases': phases,
          'file_sha256': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts
                          and p.name not in ('stage-analysis.json', 'archive-audit.json')}}
(root / 'stage-analysis.json').write_text(json.dumps(output, indent=2) + '\n')
print(json.dumps({n: {'samples': p['sample_count'], 'slow': p['slow_sample_count'],
                       'longest': p['longest_captures'][:1]}
                  for n, p in phases.items()}, indent=2))
