#!/usr/bin/env python3
"""Verify the checker repair against preserved raw v6 captures, without a VM."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
OLD = ROOT.parent / 'cpu-shares-v6'


def read(root, name):
    path = root / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())


for root in (ROOT, OLD):
    for name, digest in json.loads(read(root, 'archive-sha256.json')).items():
        assert hashlib.sha256(read(root, name)).hexdigest() == digest, name
owner = json.loads(read(ROOT, 'owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['inputs_unchanged'] and owner['before'] == owner['after']
assert owner['groups_empty'] and len(owner['groups']) == 3
assert all(value == [[], []] for value in owner['groups'].values())
assert not owner['functional_rerun'] and not owner['full_acceptance']
for name, digest in owner['before'].items():
    assert hashlib.sha256(read(ROOT, 'repo/' + name)).hexdigest() == digest
for command in owner['commands']:
    assert command['exit_code'] == 0 and not command.get('timeout')
    assert command['cleanup']['clean_shutdown']
test_log = read(ROOT, 'tests.stderr')
assert b'Ran 82 tests' in test_log and b'\nOK\n' in test_log
assert b'skipped' not in test_log

sys.path.insert(0, str(ROOT / 'repo/scripts'))
import cpu_share_checks as checks

report = json.loads(read(OLD, 'runtime/shares-v6/report.json'))
original = json.dumps(report['evidence'], sort_keys=True)
assert checks.evaluate(report['evidence'])['probe_streams']['status'] == 'fail'
assert json.dumps(report['evidence'], sort_keys=True) == original
restored = copy.deepcopy(report['evidence'])
for container in restored['containers'].values():
    container.pop('stage_snapshots', None)
for update in restored['updates']:
    for side, prefix in (('pre', 'preupd'), ('post', 'postupd')):
        name = f"{prefix}-{update['container']}-{update['stage']}.stdout"
        update[side] = json.loads(read(OLD, 'runtime/shares-v6/' + name))
before = json.dumps(restored, sort_keys=True)
first = checks.evaluate(restored)
second = checks.evaluate(restored)
assert first == second == report['checks']
assert json.dumps(restored, sort_keys=True) == before
assert checks.overall_verdict(first, report['errors']) == 'indeterminate'
saved = json.loads(read(ROOT, 'rehydrated/v6-rehydration.json'))
assert saved['is_reconstruction_not_rerun']
corrected = saved['corrected']
assert corrected['rehydrated_rows_match_original_17']
assert corrected['evaluate_twice_identical']
assert corrected['input_byte_identical_before_after']
assert corrected['rehydrated_verdict'] == corrected['frozen_verdict'] == 'indeterminate'
assert corrected['rehydrated_17'] == {key: value['status'] for key, value in first.items()}
print('PASS: 82 remote tests, 3 empty groups, source hashes stable; raw replay is pure and repeats all 17 rows.')
print('Functional verdict remains indeterminate; this is a checker repair and evidence replay, not a new Docker run.')
