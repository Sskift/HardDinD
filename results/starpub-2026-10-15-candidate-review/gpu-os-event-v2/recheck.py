#!/usr/bin/env python3
"""Replay native ABI calibration with its actual per-card request."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())


for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
for name, digest in json.loads(read('source-manifest.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-report.json'))
assert owner['status'] == 'finished' and owner['sources_unchanged'] and owner['cleanup_ok']
assert len(owner['commands']) == 14
for row in owner['commands']:
    assert not row.get('timeout_expired')
    assert not row['remaining_before_cleanup'] and not row['remaining_after_cleanup']
    assert row['exit_code'] == (1 if row['name'] in {
        'native-1-check', 'native-2-check', 'native-3-check'} else 0)
assert read('inventory-before.stdout') == read('inventory-after.stdout')
assert b'Ran 36 tests' in read('fixture-tests.stderr')
spec = importlib.util.spec_from_file_location('events', ROOT / 'scripts/gpu_os_event_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
replay = json.loads(read('owner-device-recheck.json'))
assert replay['checker_sha256'] == hashlib.sha256(read('scripts/gpu_os_event_checks.py')).hexdigest()
for index in range(4):
    raw = read(f'native-{index}.stdout')
    rows = [json.loads(line) for line in raw.splitlines()]
    request = {'device': f'/dev/nvidia{index}', 'ctl_device': '/dev/nvidiactl', 'poll_ms': 30}
    expected = replay['results'][str(index)]
    assert expected['request'] == request
    assert expected['stdout_sha256'] == hashlib.sha256(raw).hexdigest()
    verdict = checks.analyze(raw.decode(), request)
    assert verdict == expected['verdict'] and verdict['ok']
    summary = rows[-1]
    assert summary['clients_created'] == summary['clients_freed'] == 9
    assert summary['allocations_ok'] == summary['frees_ok'] == 7
    assert summary['fds_opened'] == summary['fds_closed'] == 3
    original = json.loads(read(f'native-{index}-check.stdout'))
    assert original['reasons'] == ([] if index == 0 else [f'meta device /dev/nvidia{index} != /dev/nvidia0'])
print('Four unchanged native captures pass with their actual requested device; 14 groups clean.')
print('Original checker argument errors retained; internal Docker/CUDA acceptance not inferred.')
