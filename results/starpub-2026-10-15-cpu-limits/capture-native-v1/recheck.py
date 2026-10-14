"""Audit native diagnostic samples; no quota or speed acceptance is inferred."""
import gzip
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
report = json.loads((root / 'report.json').read_text())
guest_root = root.parent / 'capture-v1'
guest = json.loads((guest_root / 'runtime/report.json').read_text())
assert report['status'] == 'finished' and report['exit_code'] == 0
assert hashlib.sha256((guest_root / 'sources/src/cpu_capture_probe.c').read_bytes()).hexdigest() == report['source_sha256']
assert report['binary_sha256'] == next(v for k, v in guest['sha256'].items() if k.endswith('/cpu-limit-probe'))
cleanup = json.loads((root / 'cleanup-audit.json').read_text())
assert cleanup['all_empty'] and len(cleanup['groups']) == 2
summary = {}
for run, affinity in zip(report['runs'], ([0, 1, 2, 3], [0])):
    assert run['exit_code'] == 0 and not run['timed_out'] and run['cleanup']['clean_shutdown']
    events = [json.loads(line) for line in gzip.decompress((root / (run['name'] + '.stdout.gz')).read_bytes()).decode().splitlines()]
    assert events[0]['event'] == 'start' and events[-1]['event'] == 'end'
    assert events[0]['threads'] == 4 and events[0]['affinity'] == affinity
    assert len(events[-1]['workers']) == 4
    for worker in events[-1]['workers']:
        assert worker['affinity_start'] == affinity and worker['affinity_end'] == affinity
        assert worker['cpu_ns'] > 0 and set(worker['cpus_seen']) <= set(affinity)
    samples = [e for e in events if e['event'] == 'sample']
    assert len(samples) == 60
    for sample in samples:
        assert sample['cap_end_ns'] - sample['wall_ns'] == sample['cap_dur_ns']
        assert sample['max_stable'] == 1 and sample['stat_errno'] == 0
        assert len(sample['stages']) == 5
        previous = sample['wall_ns']
        for stage in sample['stages']:
            assert previous <= stage['begin_ns'] <= stage['work_begin_ns'] <= stage['work_end_ns'] <= stage['end_ns'] <= sample['cap_end_ns']
            assert stage['thread_cpu_begin_ns'] <= stage['thread_cpu_end_ns']
            previous = stage['end_ns']
    summary[run['name']] = {'samples': len(samples),
                            'max_capture_ms': max(e['cap_dur_ns'] for e in samples) / 1e6,
                            'observed_cpu_max': events[0]['cgroup']['cpu.max'],
                            'affinity': affinity}
audit = {'scope': report['scope'], 'summary': summary,
         'same_binary_as_guest': True, 'process_groups_empty': 2,
         'file_sha256': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in root.rglob('*') if p.is_file() and p.name != 'archive-audit.json'
                         and '__pycache__' not in p.parts}}
(root / 'archive-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
print(json.dumps(summary, indent=2))
