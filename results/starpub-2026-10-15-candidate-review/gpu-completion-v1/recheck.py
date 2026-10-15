#!/usr/bin/env python3
import gzip
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(
        p.with_name(p.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['diagnostic_complete'] and owner['normal_shutdown']
assert owner['groups_empty'] and owner['inputs_unchanged']
assert not owner['full_acceptance'] and not owner['performance_acceptance']
assert len(owner['groups']) == 29 and all(x == [[], []] for x in owner['groups'].values())
assert b'Ran 3 tests' in read('socket-tests.stderr') and b'OK' in read('socket-tests.stderr')
for phase in ('native-before', 'native-after'):
    rows = [json.loads(line) for line in read(phase + '.stdout').splitlines()]
    assert rows == owner[phase]['compute'] and owner[phase]['all_four_verified']
    assert len(rows) == 4 and {r['gpu'] for r in rows} == set(range(4))
    assert all(r['verified'] is True and r['elements'] == 1048576 for r in rows)
expected_codes = [0] * 8
expected_rows = [4] * 8
for i, case in enumerate(owner['cases']):
    label = f"case-{i}-{case['mode']}"
    assert case['mode'] == owner['sequence'][i]
    command = next(row for row in owner['commands'] if row['label'] == label)
    rows = [json.loads(line) for line in read(label + '.stdout').splitlines()]
    assert rows == case['compute'] and len(rows) == expected_rows[i]
    assert command['exit_code'] == case['exit_code'] == expected_codes[i]
    assert not case['timed_out'] and command['cleanup']['clean_shutdown']
    assert case['all_four_verified'] == (expected_codes[i] == 0)
    assert [r['gpu'] for r in rows] == list(range(expected_rows[i]))
    assert all(r['verified'] is True and r['elements'] == 1048576 for r in rows)
    if expected_codes[i] == 1:
        error = b'999' if i == 6 else b'304'
        assert b'cuCtxCreate_v2(&ctx,0,dev): CUDA error ' + error in read(label + '.stderr')
    if case['mode'] in ('strace', 'both'):
        assert b'ioctl(' in read(label + '.strace')
state = owner['final_state']
assert state['phase'] == 'stopped' and state['guest_exit_code'] == 0
assert not state['forced_shutdown'] and 'shutdown_error' not in state
assert 'uml_dir=uml/' in state['command']
assert 'gpu_async_wait=1' in state['command']
assert state['config']['kernel_sha256'] == 'b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502'
assert hashlib.sha256(read('repo/scripts/harddind.py')).hexdigest() == owner['input_sha256'][
    '/root/harddind-rebuild/gpu-completion-owner-v1/repo/scripts/harddind.py']

prep = json.loads(read('build-status.json'))
assert prep['status'] == 'finished' and prep['exit_code'] == 0 and prep['groups_empty']
assert prep['source_build'] == prep['source_after']
assert len(prep['source_after']) == 33
assert len(prep['groups']) == 3 and all(x == [[], []] for x in prep['groups'].values())
assert [n for n in prep['source_before'] if prep['source_before'][n] != prep['source_after'][n]] == ['arch/um/kernel/skas/stub.c']
for name, digest in prep['source_after'].items():
    assert hashlib.sha256(read('source/' + name)).hexdigest() == digest
for row in prep['commands']:
    assert row['exit_code'] == 0 and row['cleanup']['clean_shutdown']
assert hashlib.sha256(read('kernel.config')).hexdigest() == prep['config_before']
patch = read('0028-um-gpu-completion-ownership.patch')
assert hashlib.sha256(patch).hexdigest() == prep['patch_sha256']
assert prep['patch_sha256'] == '8a5288aeb8e3bf987269d4d3f0addf0497b9db3c890b979913bbd29463ec728a'
assert prep['kernel_sha256'] == state['config']['kernel_sha256']
old = ROOT.parent / 'gpu-mapping-v14/source/arch/um/kernel/skas/stub.c'
assert hashlib.sha256(old.read_bytes()).hexdigest() == prep['source_before']['arch/um/kernel/skas/stub.c']
with tempfile.TemporaryDirectory() as tmp:
    tree = Path(tmp)
    target = tree / 'arch/um/kernel/skas/stub.c'
    target.parent.mkdir(parents=True)
    shutil.copy2(old, target)
    applied = subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1'],
                             cwd=tree, input=patch, capture_output=True)
    assert applied.returncode == 0, applied.stderr
    assert target.read_bytes() == read('source/arch/um/kernel/skas/stub.c')
print('Only stub.c changed; exact patch replay and immutable 33-file build; 3 build groups empty.')
print('Async enabled: 8/8 containers complete four-card compute, native brackets pass, 29 groups empty.')
print('Normal shutdown; no overall GPU stability/performance acceptance from this finite matrix.')
