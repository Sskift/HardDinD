#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 0
assert r['groups_empty'] and len(r['groups']) == 4
assert all(value == [[], []] for value in r['groups'].values())
assert r['source_unchanged'] and r['source_before'] == r['source_after']
for name, digest in r['source_before'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest, name
assert [(c['label'], c['exit_code']) for c in r['commands']] == [
    ('network-build', 0), ('main-check', 0), ('backend-tests', 0), ('probe-addresses', 0)]
assert all(c['cleanup']['clean_shutdown'] and not c.get('timeout') for c in r['commands'])
main = read('main-check.stderr')
assert b'Ran 219 tests in ' in main and b'OK (skipped=13)' in main
assert main.count(b'... skipped ') == 13
assert b'Ran 29 tests in ' in read('backend-tests.stderr')
assert b'\nOK\n' in read('backend-tests.stderr')
assert b'NATIVE IP6 FRAG TESTS: ALL PASS' in read('network-build.stdout')
assert b'4 127.0.0.1\n' in read('probe-addresses.stdout')
assert b'6 ::1\n' in read('probe-addresses.stdout')
gate = ROOT.parent / 'publish-fragment-docker-v1' / 'post-run-audit.json'
assert r['docker_gate_sha256'] == hashlib.sha256(gate.read_bytes()).hexdigest()
assert not r['full_acceptance'] and not r['performance_acceptance']
print('PASS: integrated build, static probe, 206 main tests passed / 13 skipped,')
print('29 backend tests passed, source unchanged and four process groups empty.')
