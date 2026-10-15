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
assert r['groups_empty'] and r['inputs_unchanged']
assert len(r['groups']) == 7 and all(v == [[], []] for v in r['groups'].values())
assert r['sources_before'] == r['sources_after']
for name, digest in r['sources_before'].items():
    assert hashlib.sha256(read('repo/lifecycle/' + name)).hexdigest() == digest
assert r['phase_pass'] == {'native-before': False}
assert r['final_state'] == {} and not r['normal_shutdown']
assert [c['label'] for c in r['commands']] == ['compile', 'fixture-compile', 'args-fixture', 'checker-tests', 'inventory', 'native-before', 'native-before-check']
assert [c['exit_code'] for c in r['commands']] == [0, 0, 0, 0, 0, 1, 1]
assert all(not c.get('timeout') and c['cleanup']['clean_shutdown'] for c in r['commands'])
tests = read('checker-tests.stderr')
assert b'Ran 46 tests' in tests and b'OK (skipped=1)' in tests
assert b"skipped 'args fixture binary not built'" in tests
rows, invalid = [], []
for number, line in enumerate(read('native-before.stdout').splitlines(), 1):
    try:
        rows.append(json.loads(line))
    except ValueError:
        invalid.append(number)
assert invalid == [2, 3, 4, 5, 23, 24, 25, 26]
errors = [x for x in rows if x.get('type') == 'error' and x.get('stage') == 'solo_load']
assert len(errors) == 4 and all(x['message'].endswith('rc=218') for x in errors)
solo = [x for x in rows if x.get('type') == 'worker' and x.get('phase') == 'solo']
assert len(solo) == 4 and all(x['info_enter_rc'] == 201 and x['allocs'] == 0 for x in solo)
assert [x['status'] for x in rows if x.get('type') == 'summary'] == ['fail']
check = json.loads(read('native-before-check.stdout'))
assert check['ok'] is False and 'summary not clean' in check['reasons']
print('PASS: native calibration failure, 8 malformed JSON lines, module-load errors and 1 test skip retained.')
print('No guest/Docker test ran; 7 groups empty and source identity unchanged.')
