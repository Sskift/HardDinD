#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 1 and r['diagnostic_only']
assert r['phase_pass'] == {'docker': False} and not r['paired_native_control']
assert not r['full_acceptance'] and not r['performance_acceptance']
assert r['normal_shutdown'] and r['final_state']['phase'] == 'stopped'
assert r['groups_empty'] and len(r['groups']) == 17
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['sources_before'] == r['sources_after']
for name, digest in r['sources_before'].items():
    assert hashlib.sha256(read('repo/lifecycle/' + name)).hexdigest() == digest
assert len(r['commands']) == 14
for command in r['commands']:
    assert command['exit_code'] == {'docker': -9, 'docker-check': 1}.get(command['label'], 0)
    assert bool(command.get('timeout')) == (command['label'] == 'docker')
    assert command['cleanup']['clean_shutdown']
config = r['final_state']['config']
assert config['cpus'] == 4 and config['gpu_async_wait']
assert config['kernel_sha256'] == 'b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502'
second = json.loads(read('second-observer.json'))
assert second['exit_code'] == 0 and not second.get('timeout') and second['cleanup']['clean_shutdown']
audit = json.loads(read('post-run-audit.json'))
assert len(audit['groups']) == 18 and audit['groups_empty']
assert all(value == [[], []] for value in audit['groups'].values())
assert audit['groups'][str(second['pid'])] == [[], []]
binding = json.loads(read('lock-cycle-source-binding.json'))
assert binding['upstream_archive_sha256'] == '2feaa70453e91f541be6a5c9078d8168e081af45a3d9718054850538cff2ebf3'
assert hashlib.sha256(read('lock-cycle-source-binding.json')).hexdigest() == audit['source_binding_sha256']
for name, item in binding['files'].items():
    assert hashlib.sha256(read('source/' + name)).hexdigest() == item['sha256']
snapshots = []
for name in ['diagnostic-proc.stdout', 'second-observer.stdout']:
    rows = [json.loads(line) for line in read(name).splitlines()]
    stacks = {row['tid']: row for row in rows if row['field'] == 'stack'}
    assert len(stacks) == 10 and all(row['errno'] == 0 for row in stacks.values())
    assert all(term in stacks[12]['data'] for term in [
        'gpu_forward_block', 'fault_in_readable', 'handle_page_fault', 'rwsem_down_read_slowpath'])
    assert all(term in stacks[14]['data'] for term in [
        'vm_mmap_pgoff', '__mmap_region', 'gpu_vma_close', 'mutex_lock'])
    assert 'gpu_forward_block' in stacks[13]['data'] and 'mutex_lock' in stacks[13]['data']
    snapshots.append(stacks)
assert snapshots[1][12]['at_ns'] - snapshots[0][12]['at_ns'] > 120_000_000_000
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / 'docker.jsonl').write_bytes(read('docker.stdout'))
    (tmp / 'reference.jsonl').write_bytes(read('reference-native-before.stdout'))
    check = subprocess.run([sys.executable, str(ROOT / 'repo/lifecycle/scripts/cuda_lifecycle_checks.py'),
                            str(tmp / 'docker.jsonl'), '--uuids', ','.join(r['expected_uuids']),
                            '--native-reference', str(tmp / 'reference.jsonl')],
                           capture_output=True, text=True, timeout=30)
    assert check.returncode == 1 and json.loads(check.stdout) == json.loads(read('docker-check.stdout'))
print('PASS: two actual guest snapshots preserve the same GPU / mmap lock cycle over 120 seconds apart.')
print('Docker thread timeout retained; normal shutdown, 18 groups empty, fixed inputs unchanged.')
print('Focused diagnostic only: no repaired-kernel, current paired-native or performance acceptance.')
