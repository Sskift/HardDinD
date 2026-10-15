#!/usr/bin/env python3
"""Recheck the raw ptrace ABI observations and unchanged kernel matrix."""
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(root, name):
    path = root / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())


def load(root, name):
    return json.loads(read(root, name))


def verify_hashes(root):
    for name, digest in load(root, 'archive-sha256.json').items():
        assert hashlib.sha256(read(root, name)).hexdigest() == digest, name
    owner = load(root, 'owner-status.json')
    for name, digest in owner['sources'].items():
        assert hashlib.sha256(read(root, 'repo/' + name)).hexdigest() == digest, name
    return owner


owner = verify_hashes(ROOT)
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['inputs_unchanged'] and owner['all_cells_passed'] and owner['groups_empty']
assert not owner['full_acceptance'] and not owner['performance_acceptance']
cases = {
    'gp-syscall', 'fp-preserve', 'fp-signal-preserve', 'fp-signal-edit',
    'gp-signal-edit', 'fp-nested', 'fp-threads-1', 'fp-threads-4',
    'frame-churn', 'thread-exit-churn', 'runner-lifetime', 'fork-fp',
    'exec-reset', 'ptrace-prefix-semantics', 'ptrace-gp-fp',
}
cells = {
    f'{kernel}-{cpus}cpu-p{parallel}-l{lazy}'
    for kernel in ('eager', 'lazy') for cpus in (1, 4) for parallel in (0, 1)
    for lazy in ((0,) if kernel == 'eager' else (0, 1))
}
assert set(owner['cells']) == cells
assert owner['probe_sha256'] == '31566981ad31367c4fa3eab7fb69384580296b452774626c19f59692c25f8dec'
for label, verdict in {'native': owner['native'], **owner['cells']}.items():
    text = read(ROOT, label + '.stdout').decode()
    rows = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
    assert Counter(row['case'] for row in rows) == Counter(cases), label
    assert rows == verdict['rows'] and verdict['verdict'] == 'pass', label
    assert not verdict['errors'] and not verdict['skipped'], label
    assert all(r['status'] == 'pass' and r['ok'] is True for r in rows), label
    by_case = {r['case']: r for r in rows}
    assert 'post-wake FP/SIMD intact' in by_case['frame-churn']['detail']
    assert 'child asserts inherited bytes first' in by_case['fork-fp']['detail']
    records = by_case['ptrace-prefix-semantics']['records']
    assert [x['requested_len'] for x in records] == [28, 32, 256, 172, 176, 520]
    assert all(x['post_iov_len'] == x['requested_len'] for x in records)
    for rec in records:
        rejected = label == 'native' or rec['tag'] in ('FX28', 'XS172')
        assert rec['raw_rc'] == (-1 if rejected else 0)
        assert rec['errno'] == ((14 if rec['tag'] in ('XS176', 'XS520') else 22) if rejected else 0)
        assert rec['outcome'] == ('atomic-reject' if rejected else 'retained')
    if label != 'native':
        for prefix, suffix in (
                ('HARDDIND_FP_PROBE_SHA256=', owner['probe_sha256']),
                ('HARDDIND_FP_PROBE_RC=', '0'),
                ('HARDDIND_FP_END ', 'status=0')):
            assert [s for s in text.splitlines() if s.startswith(prefix)] == [prefix + suffix]
        assert verdict['kernel_unchanged']
audit = load(ROOT, 'owner-cleanup-audit.json')
assert len(owner['commands']) == len(audit) == 17
assert set(audit) == {str(r['pid']) for r in owner['commands']}
assert all(v == [[], []] for v in audit.values())
for row in owner['commands']:
    assert row['exit_code'] == 0 and not row['timed_out']
    assert row['cleanup']['clean_shutdown'] and not row['cleanup']['forced_cleanup']
print('Raw ptrace ABI native 15/15 and 12 x 15 guest diagnostics verified; rejected iov lengths retained.')
print('All 17 groups clean; ABI parity, coredump coverage and performance remain unproven.')
