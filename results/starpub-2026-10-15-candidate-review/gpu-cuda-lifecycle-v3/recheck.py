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
assert r['phase_pass'] == {'native-before': True, 'docker': False, 'native-after': True}
assert not r['full_acceptance'] and not r['performance_acceptance']
assert r['normal_shutdown'] and r['final_state']['phase'] == 'stopped'
assert r['groups_empty'] and len(r['groups']) == 19
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['sources_before'] == r['sources_after']
for name, digest in r['sources_before'].items():
    assert hashlib.sha256(read('repo/lifecycle/' + name)).hexdigest() == digest
assert len(r['commands']) == 16
for command in r['commands']:
    expected = {'docker': -9, 'docker-check': 1}.get(command['label'], 0)
    assert command['exit_code'] == expected
    assert bool(command.get('timeout')) == (command['label'] == 'docker')
    assert command['cleanup']['clean_shutdown']
assert json.loads(read('args-fixture.stdout')) == {'case': 'fixture_summary', 'checks': 45, 'failures': 0}
unit = read('checker-tests.stderr')
assert b'Ran 62 tests in ' in unit and b'OK (skipped=6)' in unit
assert unit.count(b"skipped 'emitter fixture not built'") == 6
config = r['final_state']['config']
assert config['cpus'] == 4 and config['gpu_async_wait']
assert config['kernel_sha256'] == 'b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502'
assert b'obs[i2].firstbyte_ns<0' in read('repo/lifecycle/src/cuda_cuda_lifecycle_probe.c')
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for phase in ['native-before', 'docker', 'native-after']:
        raw = read(phase + '.stdout')
        log = tmp / (phase + '.jsonl')
        log.write_bytes(raw)
        argv = [sys.executable, str(ROOT / 'repo/lifecycle/scripts/cuda_lifecycle_checks.py'),
                str(log), '--uuids', ','.join(r['expected_uuids'])]
        if phase != 'native-before':
            argv.extend(['--native-reference', str(tmp / 'native-before.jsonl')])
        check = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        assert check.returncode == (1 if phase == 'docker' else 0)
        assert json.loads(check.stdout) == json.loads(read(phase + '-check.stdout'))
        rows = [json.loads(line) for line in raw.splitlines()]
        if phase == 'docker':
            assert len(rows) == 9
            workers = [row for row in rows if row['type'] == 'worker']
            assert len(workers) == 4 and {row['gpu'] for row in workers} == {0, 1, 2, 3}
            assert all(row['phase'] == 'solo' and row['completed_iterations'] == 8
                       and row['allocs'] == row['frees'] == 24 and row['mismatches'] == 0
                       for row in workers)
        else:
            assert len(rows) == 30
            assert sum(row['type'] == 'peer' for row in rows) == 12
print('PASS: preserved Docker thread-phase timeout; both native controls pass the frozen checker.')
print('Docker solo: 32 checked cycles / 96 allocations and frees; later phases incomplete.')
print('16 command outcomes, 19 empty process groups, stable inputs; no full or performance acceptance.')
