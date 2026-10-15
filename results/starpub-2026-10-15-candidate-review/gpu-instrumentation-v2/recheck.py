#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(
        p.with_name(p.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['diagnostic_complete'] and owner['normal_shutdown']
assert owner['groups_empty'] and owner['inputs_unchanged']
assert not owner['full_acceptance'] and not owner['performance_acceptance']
assert len(owner['groups']) == 29 and all(x == [[], []] for x in owner['groups'].values())
assert b'Ran 3 tests' in read('socket-tests.stderr') and b'OK' in read('socket-tests.stderr')
for phase in ('native-before', 'native-after'):
    rows = [json.loads(line) for line in read(phase + '.stdout').splitlines()]
    assert rows == owner[phase]['compute'] and owner[phase]['all_four_verified']
    assert len(rows) == 4 and {r['gpu'] for r in rows} == set(range(4))
    assert all(r['verified'] is True and r['elements'] == 1048576 for r in rows)
expected_codes = [1, 1, 0, 0, 0, 0, 1, 139]
expected_rows = [2, 0, 4, 4, 4, 4, 1, 1]
for i, case in enumerate(owner['cases']):
    label = f"case-{i}-{case['mode']}"
    assert case['mode'] == owner['sequence'][i]
    command = next(row for row in owner['commands'] if row['label'] == label)
    rows = [json.loads(line) for line in read(label + '.stdout').splitlines()]
    assert rows == case['compute'] and len(rows) == expected_rows[i]
    assert command['exit_code'] == case['exit_code'] == expected_codes[i]
    assert not case['timed_out'] and command['cleanup']['clean_shutdown']
    assert case['all_four_verified'] == (expected_codes[i] == 0)
    assert [r['gpu'] for r in rows] == list(range(expected_rows[i]))
    assert all(r['verified'] is True and r['elements'] == 1048576 for r in rows)
    if expected_codes[i] == 1:
        error = b'999' if i == 6 else b'304'
        assert b'cuCtxCreate_v2(&ctx,0,dev): CUDA error ' + error in read(label + '.stderr')
    if case['mode'] in ('strace', 'both'):
        assert b'ioctl(' in read(label + '.strace')
for i in (0, 7):
    lines = read(f'case-{i}-preload.stderr').decode().splitlines()
    assert any('request=0xc030462b' in line and ' rc=-1 errno=5' in line for line in lines)
state = owner['final_state']
assert state['phase'] == 'stopped' and state['guest_exit_code'] == 0
assert not state['forced_shutdown'] and 'shutdown_error' not in state
assert len(state['mconsole'].encode()) == 109 and 'uml_dir=uml/' in state['command']
assert state['config']['kernel_sha256'] == '9306f4e6045bb26821338c6962f8c67f887ea6f86f917524ebe4b22a25a25927'
assert hashlib.sha256(read('repo/scripts/harddind.py')).hexdigest() == owner['input_sha256'][
    '/root/harddind-rebuild/gpu-instrumentation-owner-v2/repo/scripts/harddind.py']
print('Long-path normal shutdown verified; 29 groups empty; native before/after four cards pass.')
print('GPU matrix only 4/8 complete: two CUDA304, one CUDA999 and one exit139, with partial results.')
print('Supervisor exit0 means diagnostic complete, not GPU functional/performance acceptance.')
