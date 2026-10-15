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
assert r['status'] == 'finished' and r['exit_code'] == 1
assert r['phase_pass'] == {'native-before': True}
assert r['final_state'] == {} and not r['normal_shutdown']
assert r['groups_empty'] and len(r['groups']) == 10
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['sources_before'] == r['sources_after']
for name, digest in r['sources_before'].items():
    assert hashlib.sha256(read('repo/lifecycle/' + name)).hexdigest() == digest
assert len(r['commands']) == 10
for command in r['commands']:
    assert command['exit_code'] == (1 if command['label'] in ['start', 'remaining-container'] else 0)
    assert not command.get('timeout') and command['cleanup']['clean_shutdown']
assert json.loads(read('native-before-check.stdout'))['ok']
assert b'No such file or directory' in read('instances/cuda-life-v4/supervisor.log')
assert b'/run/network-backend' in read('instances/cuda-life-v4/supervisor.log')
assert b'instance is not ready' in read('remaining-container.stderr')
assert 'AssertionError: start' in r['error'] and 'AssertionError: remaining-container' in r['error']
print('PASS: setup failure preserved; native calibration passed, no guest or internal GPU run.')
print('Ten command groups empty and recorded inputs unchanged; no acceptance claim.')
