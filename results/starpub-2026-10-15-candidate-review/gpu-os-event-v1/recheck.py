#!/usr/bin/env python3
"""Verify the frozen failed calibration; no device access or workload."""
import gzip
import hashlib
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
report = json.loads(read('owner-report.json'))
assert report['status'] == 'finished' and report['sources_unchanged']
assert report['cleanup_ok'] and len(report['commands']) == 13
for row in report['commands']:
    assert not row.get('timeout_expired'), row['name']
    assert not row['remaining_before_cleanup'], row['name']
    assert not row['remaining_after_cleanup'], row['name']
    expected = 1 if row['name'].startswith('native-') else 0
    assert row['exit_code'] == expected, row['name']
assert read('inventory-before.stdout') == read('inventory-after.stdout')
assert json.loads(read('layout-check.stdout'))['ok'] is True
for index in range(4):
    rows = [json.loads(line) for line in read(f'native-{index}.stdout').splitlines()]
    errors = [row for row in rows if row['type'] == 'error']
    assert len(errors) == 9 and all(row['stage'] == 'new_client' for row in errors)
    events = [row for row in rows if row['type'] == 'event']
    assert len(events) == 17
    assert all(row['request']['hClient'] == 0 for row in events)
    pair = [row for row in events if row['name'] == 'two_independent_clients']
    assert [row['embeddedStatus'] for row in pair] == [0, 26, 0, 40]
    summary = rows[-1]
    assert summary['status'] == 'fail' and summary['errors'] == 11
    assert summary['clients_created'] == summary['clients_freed'] == 0
    assert summary['allocations_ok'] == summary['frees_ok'] == 6
    assert summary['fds_opened'] == summary['fds_closed'] == 3
    assert summary['nv0005_notification'] == 'not_tested'
    assert summary['internal_cuda'] == 0
    verdict = json.loads(read(f'native-{index}-check.stdout'))
    assert verdict['ok'] is False
    assert 'two_independent_clients allocs must use two distinct hClient values' in verdict['reasons']
print('Four native calibrations remain failed; zero valid client handles, 13 clean groups.')
