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
assert r['status'] == 'finished' and r['exit_code'] == 1
assert r['phase_pass'] == {'native-before': False}
assert r['final_state'] == {} and not r['normal_shutdown']
assert r['groups_empty'] and len(r['groups']) == 7
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['sources_before'] == r['sources_after']
for name, digest in r['sources_before'].items():
    assert hashlib.sha256(read('repo/lifecycle/' + name)).hexdigest() == digest
assert [(c['label'], c['exit_code']) for c in r['commands']] == [
    ('compile', 0), ('fixture-compile', 0), ('args-fixture', 0),
    ('checker-tests', 0), ('inventory', 0), ('native-before', 0), ('native-before-check', 1)]
assert all(c['cleanup']['clean_shutdown'] and not c.get('timeout') for c in r['commands'])
assert json.loads(read('args-fixture.stdout')) == {'case': 'fixture_summary', 'checks': 45, 'failures': 0}
unit = read('checker-tests.stderr')
assert b'Ran 62 tests in ' in unit and b'OK (skipped=6)' in unit
assert unit.count(b'skipped \'emitter fixture not built\'') == 6
raw = read('native-before.stdout')
records = [json.loads(line) for line in raw.splitlines()]
children = [x for x in records if x.get('phase') == 'child']
assert len(children) == 4
assert all(x['firstbyte_ns'] == -1 and x['received'] and x['child_exit'] == 0 for x in children)
assert all(x['completed_cycles'] == 2 and x['mismatches'] == 0 for x in children)
peers = [x for x in records if x['type'] == 'peer']
assert len(peers) == 12 and all(x['can_access'] and x['supported'] and x['mismatches'] == 0 for x in peers)
expected = json.loads(read('native-before-check.stdout'))
assert expected == {'ok': False, 'reasons': [
    f'child[{i}]: handshake timing order broken' for i in range(4)],
    'peer_accessible_pairs': 12, 'peer_total_pairs': 12}
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / 'native.jsonl'
    log.write_bytes(raw)
    check = subprocess.run([sys.executable, str(ROOT / 'repo/lifecycle/scripts/cuda_lifecycle_checks.py'),
                            str(log), '--uuids', ','.join(r['expected_uuids'])],
                           capture_output=True, text=True, timeout=30)
    assert check.returncode == 1 and json.loads(check.stdout) == expected
assert b'firstbyte_ns=obs[i].wait_ns=-1' in read('repo/lifecycle/src/cuda_cuda_lifecycle_probe.c')
assert b'&&!obs[i2].firstbyte_ns)' in read('repo/lifecycle/src/cuda_cuda_lifecycle_probe.c')
print('PASS: failed native calibration preserved, checker independently rejects four missing receipts;')
print('45 argument checks; 56 Python tests passed / 6 emitter tests skipped; seven groups empty.')
print('No internal Docker execution or performance pass is claimed.')
