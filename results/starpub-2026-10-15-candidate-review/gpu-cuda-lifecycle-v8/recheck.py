#!/usr/bin/env python3
"""Recheck frozen evidence, retaining the real GPU and coverage failures."""
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def digest(name):
    return hashlib.sha256(read(name)).hexdigest()


for name, expected in json.loads(read('archive-sha256.json')).items():
    assert digest(name) == expected, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 1 and r['diagnostic_only']
assert r['paired_native_control'] and not r['full_acceptance'] and not r['performance_acceptance']
assert r['phase_pass'] == dict.fromkeys(['native-before', 'docker', 'native-after'], False)
assert r['normal_shutdown'] and r['final_state']['phase'] == 'stopped'
assert r['final_state']['guest_exit_code'] == 0 and not r['final_state']['forced_shutdown']
assert r['groups_empty'] and len(r['groups']) == 20
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['sources_before'] == r['sources_after']
for name, expected in r['sources_before'].items():
    assert digest('repo/lifecycle/' + name) == expected, name
assert len(r['commands']) == 17
failures = {'native-before-check', 'docker', 'docker-check', 'native-after-check'}
for command in r['commands']:
    assert command['exit_code'] == int(command['label'] in failures)
    assert not command.get('timeout') and command['cleanup']['clean_shutdown']
assert not read('remaining-container.stdout').strip()
config = r['final_state']['config']
assert config['cpus'] == 4 and config['gpu_async_wait']
binding = json.loads(read('kernel-binding.json'))
assert config['kernel_sha256'] == binding['kernel_sha256'] == 'c44e0cc304dc1b806ff681140c0aa4a6ef8f351515c7590e8082767392e746e3'
assert binding['baseline_kernel_sha256'] == 'b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502'
assert digest('kernel.config') == binding['config_sha256']
assert digest('0029-um-gpu-usercopy-lock-order.patch') == binding['patch_sha256']
assert digest('author-evidence/build-status.json') == binding['build_manifest_sha256']
build = json.loads(read('author-evidence/build-status.json'))
assert build['status'] == 'finished' and build['exit_code'] == 0 and build['groups_empty']
assert all(value == [[], []] for value in build['groups'].values())
assert build['source_before'] == build['source_after'] == binding['source_after']
assert len(binding['source_after']) == 33
assert build['kernel_sha256'] == binding['kernel_sha256']
assert build['config_before'] == binding['config_sha256']
assert digest('author-evidence/build.py') == build['build_script_sha256']
assert [(c['label'], c['exit_code'], bool(c.get('timeout'))) for c in build['commands']] == [
    ('olddefconfig', 0, False), ('make-0', -15, True), ('make-1', -15, True), ('make-2', 0, False)]
assert all(c['cleanup']['clean_shutdown'] for c in build['commands'])
for name, expected in binding['source_after'].items():
    assert digest('source/' + name) == expected, name
baseline = ROOT.parent / 'gpu-completion-v1'
assert (baseline / 'kernel.config').read_bytes() == read('kernel.config')
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    target = tmp / 'arch/um/kernel/gpu.c'
    target.parent.mkdir(parents=True)
    shutil.copy2(baseline / 'source/arch/um/kernel/gpu.c', target)
    applied = subprocess.run(['patch', '-p1', '--batch', '-i', str(ROOT / '0029-um-gpu-usercopy-lock-order.patch')],
                             cwd=tmp, capture_output=True, timeout=30)
    assert applied.returncode == 0, applied.stderr
    assert target.read_bytes() == read('source/arch/um/kernel/gpu.c')
    for phase in ['native-before', 'docker', 'native-after']:
        (tmp / (phase + '.jsonl')).write_bytes(read(phase + '.stdout'))
        cmd = [sys.executable, str(ROOT / 'repo/lifecycle/scripts/cuda_lifecycle_checks.py'),
               str(tmp / (phase + '.jsonl')), '--uuids', ','.join(r['expected_uuids'])]
        if phase != 'native-before':
            cmd += ['--native-reference', str(tmp / 'native-before.jsonl')]
        checked = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert checked.returncode == 1
        assert json.loads(checked.stdout) == json.loads(read(phase + '-check.stdout'))
raw = [json.loads(line) for line in read('docker.stdout').splitlines()]
assert len(raw) == 42
workers = {p: [x for x in raw if x['type'] == 'worker' and x['phase'] == p]
           for p in ['solo', 'thread', 'child']}
assert all(len(v) == 4 for v in workers.values())
assert [x['create_rc'] for x in workers['thread']] == [304, 304, 304, 0]
assert [x['completed_cycles'] for x in workers['thread']] == [0, 0, 0, 4]
for phase, rows, count_key, count in [
        ('solo', workers['solo'], 'completed_iterations', 8),
        ('thread', workers['thread'][3:], 'completed_cycles', 4),
        ('child', workers['child'], 'completed_cycles', 2)]:
    for worker in rows:
        assert worker[count_key] == count and worker['bytes_checked'] == count * 1048576
        assert worker['allocs'] == worker['frees'] == count * 3 and worker['mismatches'] == 0
        assert all(worker[k] == 0 for k in ['create_rc', 'load_rc', 'unload_rc', 'destroy_rc'])
        assert len(worker['cycles_detail']) == count
        for cycle in worker['cycles_detail']:
            assert cycle['alloc_rc'] == cycle['free_rc'] == [0, 0, 0]
            assert all(cycle[k] == 0 for k in ['htod_rc', 'launch_rc', 'sync_rc', 'dtoh_rc', 'mismatches'])
children = workers['child']
assert len({x['child_pid'] for x in children}) == 4
assert all(x['received'] and x['exec_errno'] == x['child_exit'] == x['child_signal'] == 0
           and not x['killed'] and x['child_pid'] == x['reported_pid'] for x in children)
overlap = min(x['end_ns'] for x in children) - max(x['start_ns'] for x in children)
assert overlap == 62716928
peers = [x for x in raw if x['type'] == 'peer']
assert {(x['from'], x['to']) for x in peers} == {(a, b) for a in range(4) for b in range(4) if a != b}
assert all(x['can_access'] and x['create_a_rc'] == x['destroy_a_rc'] == 0
           and x['create_b_rc'] == 304 and x['destroy_b_rc'] == -1 for x in peers)
assert all(all(x[k] is None for k in ['enable_rc', 'htod_rc', 'dto_d_rc', 'dtoh_rc']) for x in peers)
audit = json.loads(read('post-run-audit.json'))
assert not audit['overall_pass'] and not audit['full_acceptance'] and not audit['performance_acceptance']
assert audit['normal_shutdown'] and audit['groups_empty'] and audit['inputs_unchanged']
assert audit['groups'] == r['groups'] and audit['peer_second_context_returns'] == [304] * 12
print('PASS: frozen source/config/patch/build and actual results reproduce; 20 runtime groups empty.')
print('Retained failures: 3 same-process context creates, all 12 second peer contexts, both native overlap checks.')
print('No 180-second stall in this run; 4 independent child processes computed and verified results.')
print('Diagnostic only. No full GPU, sustained concurrency or performance acceptance.')
