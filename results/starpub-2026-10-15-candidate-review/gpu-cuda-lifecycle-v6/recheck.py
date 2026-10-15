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
assert r['groups_empty'] and len(r['groups']) == 9
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['sources_before'] == r['sources_after']
for name, digest in r['sources_before'].items():
    assert hashlib.sha256(read('repo/lifecycle/' + name)).hexdigest() == digest
assert len(r['commands']) == 9
for command in r['commands']:
    assert command['exit_code'] == (1 if command['label'] == 'native-before-check' else 0)
    assert not command.get('timeout') and command['cleanup']['clean_shutdown']
expected = json.loads(read('native-before-check.stdout'))
assert expected == {'ok': False, 'reasons': ['child: no positive coexistence (overlap_ns=-527872)'],
                    'peer_accessible_pairs': 12, 'peer_total_pairs': 12}
raw = read('native-before.stdout')
rows = [json.loads(line) for line in raw.splitlines()]
threads = [row for row in rows if row.get('phase') == 'child']
assert len(threads) == 4
assert min(row['end_ns'] for row in threads) - max(row['start_ns'] for row in threads) == -527872
assert all(row['completed_cycles'] == 2 and row['mismatches'] == 0 for row in threads)
fixture = [json.loads(line) for line in read('diagnostic-fixture.stdout').splitlines()]
assert fixture and all('errno' in row and 'data' in row for row in fixture)
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / 'native.jsonl'
    log.write_bytes(raw)
    check = subprocess.run([sys.executable, str(ROOT / 'repo/lifecycle/scripts/cuda_lifecycle_checks.py'),
                            str(log), '--uuids', ','.join(r['expected_uuids'])],
                           capture_output=True, text=True, timeout=30)
    assert check.returncode == 1 and json.loads(check.stdout) == expected
print('PASS: native child-overlap calibration failure independently reproduced from raw records.')
print('Nine command groups empty, sources stable; no guest execution or acceptance claim.')
