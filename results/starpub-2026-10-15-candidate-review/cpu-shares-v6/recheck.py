#!/usr/bin/env python3
"""Replay the complete shares run without converting uncertainty into a pass."""
import gzip
import hashlib
import json
import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 2
assert owner['inputs_unchanged'] and owner['groups_empty'] and owner['normal_shutdown']
assert owner['sources_before'] == owner['sources_after']
for name, digest in owner['sources_before'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest
assert b'Ran 106 tests' in read('checker-and-real-capture-tests.stderr')
assert b'skipped' not in read('checker-and-real-capture-tests.stderr')
out = 'runtime/shares-v6/'
report = json.loads(read(out + 'report.json'))
sys.path.insert(0, str(ROOT / 'repo/scripts'))
import cpu_share_checks as checks
direct = checks.evaluate(copy.deepcopy(report['evidence']))
assert direct['probe_streams']['status'] == 'fail'
assert 'not a snapshot event' in direct['probe_streams']['reason']
# The old evaluator normalized update brackets in place before serialization.
# Restore only those original snapshots from the unchanged raw command output.
restored = copy.deepcopy(report['evidence'])
for update in restored['updates']:
    for side, prefix in (('pre', 'preupd'), ('post', 'postupd')):
        name = f"{prefix}-{update['container']}-{update['stage']}.stdout"
        update[side] = json.loads(read(out + name))
assert checks.evaluate(restored) == report['checks']
assert checks.overall_verdict(report['checks'], report['errors']) == report['verdict'] == 'indeterminate'
assert not report['errors'] and report['raw_output_index']['integrity_ok']
assert report['checks']['phase_2to1']['status'] == 'indeterminate'
assert report['checks']['phase_reversed_1to2']['status'] == 'indeterminate'
assert report['checks']['phase_equal_control']['status'] == 'pass'
assert report['checks']['phase_ordering']['status'] == 'not_tested'
launches = [json.loads(x) for x in read(out + 'command-launches.jsonl').splitlines()]
metadata = [json.loads(x) for x in read(out + 'command-metadata.jsonl').splitlines()]
assert metadata == report['commands'] and len(launches) == len(metadata) == 46
audit = json.loads(read('owner-cleanup-audit.json'))
assert len(audit) == 51 and all(x == [[], []] for x in audit.values())
for launched, done in zip(launches, metadata):
    assert launched['label'] == done['label'] and launched['command'] == done['command']
    assert launched['pid'] == done['cleanup']['process_group']
    assert str(launched['pid']) in audit
    assert done['cleanup']['clean_shutdown'] and not done['timed_out']
for container in ('a', 'b'):
    raw = checks.parse_events(read(out + f'logs-{container}.stdout').decode())
    assert raw == report['evidence']['containers'][container]['spin_events']
samples = [x for x in report['evidence']['containers']['a']['spin_events'] if x.get('event') == 'sample']
assert max((x['cap_end_ns'] - x['wall_ns']) / 1e9 for x in samples) == 11.490377984
assert all(x['sha256_before'] == x['sha256_after'] for x in report['sha256_inputs'])
print('Shares v6: 46 command records intact, 51 groups empty, normal stop; 106 preflight checks passed.')
print('Unequal-weight phases indeterminate, equal control passes; 11.490377984 s capture preserved.')
print('Replay restores raw brackets because the original evaluator mutated serialized evidence.')
