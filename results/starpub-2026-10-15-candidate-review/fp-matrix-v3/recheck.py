#!/usr/bin/env python3
"""Recheck the completed stronger matrix and the preceding failed preparation."""
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


failed = ROOT.parent / 'fp-matrix-v2'
old = verify_hashes(failed)
assert old['status'] == 'finished' and old['exit_code'] == 1
assert old['cells'] == {} and 'native' not in old
assert [(r['label'], r['exit_code']) for r in old['commands']] == [
    ('strict-build', 0), ('build-disk', 1)]
assert b'(build the standard probe root first)' in read(failed, 'build-disk.stderr')
assert all(v == [[], []] for v in load(failed, 'owner-cleanup-audit.json').values())

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
assert owner['probe_sha256'] == '483b66e64a392b3c6947009cb9440c696022036c12d7141489c32fe883a02b8c'
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
    if label != 'native':
        for prefix, suffix in (
                ('HARDDIND_FP_PROBE_SHA256=', owner['probe_sha256']),
                ('HARDDIND_FP_PROBE_RC=', '0'),
                ('HARDDIND_FP_END ', 'status=0')):
            assert [s for s in text.splitlines() if s.startswith(prefix)] == [prefix + suffix]
        assert verdict['kernel_unchanged']
audit = load(ROOT, 'owner-cleanup-audit.json')
assert len(owner['commands']) == len(audit) == 15
assert set(audit) == {str(r['pid']) for r in owner['commands']}
assert all(v == [[], []] for v in audit.values())
for row in owner['commands']:
    assert row['exit_code'] == 0 and not row['timed_out']
    assert row['cleanup']['clean_shutdown'] and not row['cleanup']['forced_cleanup']
print('Prior preparation failure retained. Stronger native 15/15 and 12 x 15 guest cases verified.')
print('All 15 groups clean; ABI parity, coredump coverage and performance remain unproven.')
