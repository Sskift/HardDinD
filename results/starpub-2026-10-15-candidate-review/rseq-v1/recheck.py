#!/usr/bin/env python3
"""Replay frozen rseq evidence, including failures and ineffective injections."""
import hashlib
import json
from pathlib import Path
import re
import statistics
import tarfile

base = Path(__file__).resolve().parent
repo = base.parents[2]
manifest = json.loads((base / 'export.json').read_text())
archives = {}
for name, entry in manifest['archives'].items():
    p = base / name
    assert hashlib.sha256(p.read_bytes()).hexdigest() == entry['sha256'], name
    with tarfile.open(p) as tar:
        members = tar.getmembers()
        assert all(m.isfile() for m in members)
        assert len(members) == len(entry['members'])
        assert {m.name for m in members} == set(entry['members'])
        data = {m.name: tar.extractfile(m).read() for m in members}
    for member, digest in entry['members'].items():
        assert hashlib.sha256(data[member]).hexdigest() == digest, member
    archives[name] = data

build = json.loads(archives['build.tar.gz']['build-status.json'])
assert build['status'] == 'finished' and build['exit_code'] == 0
assert build['baseline_files_verified'] and not build['lost_config']
assert set(build['config_changes']) == {
    'CONFIG_SCHED_MM_CID', 'CONFIG_RSEQ', 'CONFIG_UML_X86_RSEQ', 'CONFIG_HAVE_RSEQ'}
assert all(c['exit_code'] == 0 and c['cleanup']['clean_shutdown'] for c in build['commands'])
patch = archives['build.tar.gz']['0001-EXPERIMENTAL-um-x86-rseq-v1.patch']
assert patch == (repo / 'kernel/experiments/0029-um-opt-in-rseq.patch').read_bytes()
assert b'+\tdefault n\n' in patch
config = archives['build.tar.gz']['candidate.config']
assert b'CONFIG_RSEQ=y\n' in config and b'CONFIG_UML_X86_RSEQ=y\n' in config
assert b'CONFIG_DEBUG_RSEQ=y\n' not in config
assert not manifest['full_acceptance'] and not manifest['performance_acceptance']

reports = {}
for name in ['query.tar.gz', 'semantics-v1.tar.gz', 'semantics-v2.tar.gz', 'semantics-v3.tar.gz']:
    data = archives[name]
    state = reports[name] = json.loads(data['status.json'])
    entry = manifest['archives'][name]
    assert state['status'] == 'finished' and state['exit_code'] == entry['exit_code']
    assert state['groups_empty'] and state['inputs_unchanged']
    assert all(g == [[], []] for g in state['groups'].values())
    assert len(state['commands']) == entry['commands'] and len(state['groups']) == entry['groups']
    assert not state['full_acceptance'] and not state['performance_acceptance']
    for command in state['commands']:
        assert not command.get('timeout') and command['cleanup']['clean_shutdown']
        for stream in ['stdout', 'stderr']:
            assert hashlib.sha256(data[command['label'] + '.' + stream]).hexdigest() == command[stream + '_sha256']
    final = state['final_state']
    assert final['phase'] == 'stopped' and final['guest_exit_code'] == 0 and not final['forced_shutdown']
    assert 'ncpus=4' in final['command'] and 'seccomp_threads=1' in final['command']
    assert final['config']['gpu_control_experiment'] is False
    if 'tests' in state:
        commands = {c['label']: c for c in state['commands']}
        for label, test in state['tests'].items():
            assert test['exit_code'] == commands[label]['exit_code']
            if test['status'] == 'pass':
                assert test['exit_code'] == 0 and not test['timed_out']

query = reports['query.tar.gz']
assert query['probe_sha256'] == '25e47e787dcfaf2b816884eda4e8f5b93b667e2a1002faa357298b0c881c6faa'
assert sum(len(c['samples']) for c in query['cells'].values()) == 285
for label, cell in query['cells'].items():
    raw = archives['query.tar.gz'][label + '.stdout'].decode().splitlines()
    rows = [json.loads(x) for x in raw if x.startswith('{')]
    assert rows[0] == cell['info'] and rows[1:-1] == cell['samples']
    assert rows[-1] == {'event': 'complete', 'verified': True}
    assert cell['info']['libc_rseq_size'] == 32
    if label.startswith('guest'):
        assert raw[0].split() == [query['probe_sha256'], '/clock-query-probe']
    for row in cell['samples']:
        assert row['verified'] and row['errno'] == row['backwards'] == row['bad_cpu'] == 0
        assert row['completed'] == row['iterations'] == 20000 and row['end_raw_ns'] > row['start_raw_ns']
        assert row['allowed_cpus'] == ([2, 3] if label.endswith('cpuset') else [0, 1, 2, 3])
    print(label, {mode: round(statistics.median(
        (r['end_raw_ns'] - r['start_raw_ns']) / r['iterations']
        for r in cell['samples'] if r['mode'] == mode), 2)
        for mode in ['libc_getcpu', 'libc_clock', 'libc_clock_getcpu']})

v1 = reports['semantics-v1.tar.gz']['tests']
assert sum(t['status'] == 'fail' for t in v1.values()) == 26
v2 = reports['semantics-v2.tar.gz']['tests']
assert {label for label, t in v2.items() if t['status'] == 'fail'} == {
    'native-before-migration', 'native-after-migration'}
for label in ['native-before-migration', 'native-after-migration']:
    assert b'rseq_node_id_available()' in archives['semantics-v2.tar.gz'][label + '.stderr']
for label in [label for label in v2 if label.endswith('-signal')]:
    counts = re.findall(rb'signals delivered: (\d+)', archives['semantics-v2.tar.gz'][label + '.stdout'])
    assert counts and all(int(x) == 0 for x in counts)
print('Owner correction: v2 signal/yield injection at point 7 was ineffective; 8 nominal passes are NOT TESTED for their intended event.')
print('v2 owner classification: 26 pass, 2 fail, 8 not_tested. Original reports remain intact.')
v3 = reports['semantics-v3.tar.gz']['tests']
assert len(v3) == 12 and all(t['status'] == 'pass' for t in v3.values())
for label in v3:
    if label.endswith(('-signal', '-preempt')):
        text = archives['semantics-v3.tar.gz'][label + '.stdout'].decode()
        counts = [(int(a), int(b)) for a, b in re.findall(
            r'number of rseq abort: (\d+), signals delivered: (\d+)', text)]
        assert len(counts) == (4 if label.endswith('-signal') else 16)
        assert sum(x[1 if label.endswith('-signal') else 0] for x in counts) > 0
        print(label, {'aborts': sum(x[0] for x in counts), 'signals': sum(x[1] for x in counts)})
assert archives['semantics-v2.tar.gz']['sources/src/rseq_lifecycle_probe.c'] == (repo / 'src/rseq_lifecycle_probe.c').read_bytes()
print('Verified build, 285 query samples, 175 runtime command receipts and 187 empty groups.')
print('The signal injection is outside the assembly critical section; asynchronous signal fixup inside it remains unverified.')
print('Original CPU work and real Docker workflow A/B runs remain required. No overall acceptance.')
