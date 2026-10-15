#!/usr/bin/env python3
"""Verify the archived query diagnostic; never grants Docker acceptance."""
import hashlib
import json
from pathlib import Path
import statistics
import tarfile

base = Path(__file__).resolve().parent
manifest = json.loads((base / 'export.json').read_text())
archive = base / 'evidence.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['sha256']
with tarfile.open(archive) as tar:
    members = tar.getmembers()
    assert len(members) == len(manifest['members'])
    assert all(m.isfile() for m in members)
    assert {m.name for m in members} == set(manifest['members'])
    data = {m.name: tar.extractfile(m).read() for m in members}
for name, digest in manifest['members'].items():
    assert hashlib.sha256(data[name]).hexdigest() == digest, name
state = json.loads(data['status.json'])
assert state['status'] == 'finished' and state['exit_code'] == 0
assert state['groups_empty'] and state['inputs_unchanged']
assert not state['full_acceptance'] and not state['performance_acceptance']
assert all(value == [[], []] for value in state['groups'].values())
assert len(state['commands']) == manifest['commands'] == 25
assert len(state['groups']) == manifest['groups'] == 28
for command in state['commands']:
    assert command['exit_code'] == 0 and not command.get('timeout')
    assert command['cleanup']['clean_shutdown']
    assert not command['cleanup']['forced_cleanup']
    for stream in ('stdout', 'stderr'):
        assert hashlib.sha256(data[command['label'] + '.' + stream]).hexdigest() == command[stream + '_sha256']
final = state['final_state']
assert final['phase'] == 'stopped' and final['guest_exit_code'] == 0
assert not final['forced_shutdown']
assert 'ncpus=4' in final['command'] and 'seccomp_threads=1' in final['command']
assert not final['config']['gpu_control_experiment']
cells = {'native-before-1': (1, [0, 1, 2, 3]),
         'native-before-4': (4, [0, 1, 2, 3]),
         'guest-1': (1, [0, 1, 2, 3]),
         'guest-4': (4, [0, 1, 2, 3]),
         'guest-4-cpuset': (4, [2, 3]),
         'native-after-1': (1, [0, 1, 2, 3]),
         'native-after-4': (4, [0, 1, 2, 3])}
assert set(cells) == set(state['cells'])
modes = {'libc_clock', 'syscall_clock', 'libc_getcpu',
         'syscall_getcpu', 'libc_clock_getcpu'}
total = 0
for label, (threads, cpus) in cells.items():
    lines = data[label + '.stdout'].decode().splitlines()
    rows = [json.loads(line) for line in lines if line.startswith('{')]
    cell = state['cells'][label]
    assert rows[0] == cell['info'] and rows[-1] == {'event': 'complete', 'verified': True}
    assert rows[1:-1] == cell['samples'] and len(rows) == threads * 15 + 2
    assert {(r['mode'], r['repeat'], r['thread']) for r in cell['samples']} == {
        (mode, rep, thread) for mode in modes for rep in range(3) for thread in range(threads)}
    if label.startswith('guest'):
        assert lines[0].split() == [state['probe_sha256'], '/clock-query-probe']
    for row in cell['samples']:
        assert row['verified'] and row['errno'] == row['bad_cpu'] == row['backwards'] == 0
        assert row['completed'] == row['iterations'] == 20000
        assert row['allowed_cpus'] == cpus and row['end_raw_ns'] > row['start_raw_ns']
        assert row['rseq_size'] == (0 if label.startswith('guest') else 32)
    total += len(cell['samples'])
    medians = {mode: round(statistics.median(
        (r['end_raw_ns'] - r['start_raw_ns']) / r['iterations']
        for r in cell['samples'] if r['mode'] == mode), 2) for mode in sorted(modes)}
    print(label, json.dumps(medians, sort_keys=True))
assert total == manifest['samples'] == 285
assert hashlib.sha256(data['sources/cpu_limit_probe.c']).hexdigest() == manifest['original_cpu_limit_probe_sha256']
print('Verified 7 cells, 285 thread samples, 25 commands, 28 empty process groups.')
print('Durations are guest/native CLOCK_MONOTONIC_RAW per thread, in ns per loop iteration.')
print('No whole-Docker or performance acceptance; external binaries remain hash-bound on Starpub.')
