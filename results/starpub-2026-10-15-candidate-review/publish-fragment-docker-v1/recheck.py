#!/usr/bin/env python3
"""Check the real Docker report and its separate post-run cleanup audit."""
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())


manifest = json.loads(read('archive-sha256.json'))
for name, digest in manifest.items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
r = json.loads(read('runtime/pubfrag1.json'))
audit = json.loads(read('post-run-audit.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 1
assert "KeyError: 'pgid'" in owner['cleanup_error']
assert owner['commands'][0]['exit_code'] == 0
assert owner['commands'][0]['cleanup']['clean_shutdown']
assert audit['owner_sha256'] == hashlib.sha256(read('owner-status.json')).hexdigest()
assert audit['runtime_report_sha256'] == hashlib.sha256(read('runtime/pubfrag1.json')).hexdigest()
assert r['subset_passed'] and len(r['checks']) == 21
assert all(value == 'pass' for value in r['checks'].values())
assert not r['full_acceptance'] and not r['performance_acceptance']
assert audit['audit_passed'] and audit['functional_subset_passed']
assert audit['groups_empty'] and len(audit['groups']) == 128
assert all(value == [[], []] for value in audit['groups'].values())
assert r['inputs_unchanged'] and audit['inputs_unchanged']
assert r['sha256'] == r['sha256_after'] == audit['sha256_after']
assert audit['source_unchanged'] and owner['source_before'] == audit['source_after']
for name, digest in audit['source_after'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest, name
assert len(r['commands']) == 121
assert sum(c['exit_code'] != 0 for c in r['commands']) == 9
for command in r['commands']:
    assert command['expected_exit_satisfied'] and not command['timed_out']
    assert command['cleanup']['clean_shutdown']
    assert audit['groups'][str(command['cleanup']['process_group'])] == [[], []]
    for stream in ['stdout', 'stderr']:
        assert 'runtime/' + command[stream] in manifest
assert len(r['echoes']) == 140
assert all(e['verified'] and e['sent_bytes'] == e['received_bytes'] for e in r['echoes'])
sizes = [1, 1200, 1452, 1453, 1456, 8000, 60000, 65507]
v6_udp = Counter(e['sent_bytes'] for e in r['echoes'] if e['host'] == '::1' and e['protocol'] == 'udp')
assert v6_udp == Counter({size: 2 for size in sizes})
assert any(e['host'] == '::1' and e['protocol'] == 'tcp' and e['sent_bytes'] == 65536 for e in r['echoes'])
for state in [r['first_run'], r['final_run']]:
    assert state['phase'] == 'stopped' and state['guest_exit_code'] == 0
    assert not state['forced_shutdown']
    assert state['guest_cleanup']['clean_shutdown'] and state['network_cleanup']['clean_shutdown']
    for key in ['guest_pid', 'network_pid', 'supervisor_pid']:
        assert audit['groups'][str(state[key])] == [[], []]
native = ROOT.parent / 'publish-fragment-native-v2' / 'owner-status.json'
assert owner['native_gate_sha256'] == hashlib.sha256(native.read_bytes()).hexdigest()
native_report = json.loads(native.read_text())
backend = next(digest for path, digest in r['sha256'].items() if path.endswith('/network-backend'))
assert backend == native_report['backend_sha256']['release']
print('PASS: 21 Docker publication checks, 140 verified echoes, 121 expected command outcomes;')
print('both VM runs stopped normally, 128 groups empty, source/binary/disk inputs unchanged.')
print('Wrapper KeyError is preserved; post-run audit independently verifies cleanup.')
print('No full Docker, full IPv6 or performance acceptance is inferred.')
