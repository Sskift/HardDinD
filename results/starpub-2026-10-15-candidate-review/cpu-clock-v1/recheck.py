#!/usr/bin/env python3
from pathlib import Path
import ast, gzip, hashlib, importlib.util, json, statistics, subprocess, sys, tempfile

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent

def read(name):
    p = ROOT / name
    return p.read_bytes() if p.is_file() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())

def obj(name):
    return json.loads(read(name))

archive = obj('archive-sha256.json')
for name, digest in archive.items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = obj('owner-status.json')
prepared = obj('prepare.json')
audit = obj('post-run-audit.json')
assert r['status'] == 'finished' and r['exit_code'] == 0
assert r['groups_empty'] and r['inputs_unchanged'] and r['external_inputs_unchanged']
assert not any(r[k] for k in ['full_acceptance', 'performance_acceptance', 'resource_enforcement_acceptance'])
assert len(r['commands']) == 32 and len(r['groups']) == 47
assert all(c['exit_code'] == 0 and not c.get('timeout') and c['cleanup']['clean_shutdown'] for c in r['commands'])
assert all(v == [[], []] for v in r['groups'].values())
assert audit['execution_pass'] and audit['groups_empty'] and audit['inputs_unchanged'] and audit['external_inputs_unchanged']
assert len(audit['groups']) == 47 and all(v == [[], []] for v in audit['groups'].values())
assert r['inputs'] == prepared['files']
assert set(prepared['files']) - set(archive) == {'baseline-kernel', 'candidate-kernel', 'cpu-capture-witness', 'rootfs.tar'}
for name, digest in prepared['files'].items():
    if name in archive:
        assert hashlib.sha256(read(name)).hexdigest() == digest, name
assert read('baseline.config') == read('candidate.config')
assert prepared['config_identical'] and prepared['source_reapplication_matches']
for name, digest in obj('baseline-patches.json')['files'].items():
    assert hashlib.sha256(read('source/' + name)).hexdigest() == digest, name
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for p in (ROOT / 'source').rglob('*'):
        if p.is_file():
            q = tmp / p.relative_to(ROOT / 'source');q.parent.mkdir(parents=True, exist_ok=True);q.write_bytes(p.read_bytes())
    for row in prepared['apply_logs']:
        patch = read('author-evidence/' + row['patch'])
        assert hashlib.sha256(patch).hexdigest() == row['sha256'] and row['exit_code'] == 0
        result = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-d', str(tmp)], input=patch, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
    assert len(prepared['reapplied_sha256']) == 35
    for name, digest in prepared['reapplied_sha256'].items():
        assert hashlib.sha256((tmp / name).read_bytes()).hexdigest() == digest, name
        assert hashlib.sha256(read('reapplied/' + name)).hexdigest() == digest, name

spec = importlib.util.spec_from_file_location('clock_witness_check', ROOT / 'host_correlate.py')
checker = importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
tree = ast.parse(read('supervisor.py').decode())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'analyze')
labels = ['native-before', 'baseline', 'off-before', 'on-first', 'on-second', 'off-after', 'native-after']
assert list(r['cells']) == labels
count = 0
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    env = {'b': tmp, 'json': json, 'statistics': statistics, 'checker': checker}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'supervisor.py', 'exec'), env)
    for label in labels:
        cell = r['cells'][label]
        assert cell['completed']
        native = label.startswith('native-')
        assert list(cell['samples']) == (['unlimited'] if native else ['quota', 'unlimited'])
        for workload, old in cell['samples'].items():
            sample = label + '-' + workload
            for suffix in ['.stdout', '.stderr']:
                (tmp / (sample + suffix)).write_bytes(read(sample + suffix))
            result = env['analyze'](sample, None if native else workload == 'quota')
            assert result == old, sample
            count += result['capture']['count']
        if not native:
            st = r['states']['clock-' + label]
            assert cell['normal_shutdown'] and st['phase'] == 'stopped' and st['guest_exit_code'] == 0 and not st['forced_shutdown']
            cmd = cell['ready_state']['command']
            assert 'ncpus=4' in cmd and 'seccomp_threads=1' in cmd
            mode = None if label == 'baseline' else '1' if label.startswith('on-') else '0'
            assert [x for x in cmd if x.startswith('harddind_hr_sched_clock=')] == ([] if mode is None else ['harddind_hr_sched_clock=' + mode])
assert count == 720
assert len(obj('parser-fixtures.json')['pass']) == 6 and not obj('parser-fixtures.json')['fail']
print('PASS: 720 raw captures / 3600 stages replayed; 35 source files reproduced; 32 commands and 47 empty groups.')
print('Quiet-observer diagnosis only; no clock-fix causality, busy-load enforcement or Docker performance acceptance.')
