#!/usr/bin/env python3
"""Recheck archived native evidence without rerunning or repairing it."""
import gzip
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent


def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())


manifest = json.loads(read('archive-sha256.json'))
for name, digest in manifest.items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 0
assert r['groups_empty'] and len(r['groups']) == 12
assert all(value == [[], []] for value in r['groups'].values())
assert r['inputs_unchanged'] and r['source_before'] == r['source_after']
assert r['independent_before'] == r['independent_after']
assert not r['full_acceptance'] and not r['nested_docker_test']
expected = []
for variant in ['release', 'asan']:
    for suffix in ['build', 'independent-build', 'independent-tests',
                   'negative-build', 'negative-tests', 'backend-tests']:
        expected.append((variant + '-' + suffix, 1 if suffix == 'negative-tests' else 0))
    good = read(variant + '-independent-tests.stdout')
    bad = read(variant + '-negative-tests.stdout')
    assert b'SUMMARY checks=108 failures=0 bug_conditions_observed=0' in good
    assert b'SKIP' not in good and b'  FAIL [' not in good
    assert b'SUMMARY checks=102 failures=20 bug_conditions_observed=17' in bad
    assert b'SKIP dependent refill/fresh-TTL steps' in bad
    assert b'NATIVE IP6 FRAG TESTS: ALL PASS' in read(variant + '-build.stdout')
    backend = read(variant + '-backend-tests.stderr')
    assert b'Ran 29 tests in ' in backend and b'\nOK\n' in backend
    assert b'skipped=' not in backend and b'... skipped' not in backend
    for label in ['build', 'independent-tests', 'negative-tests', 'backend-tests']:
        raw = read(variant + '-' + label + '.stderr')
        assert b'ERROR: AddressSanitizer' not in raw and b'runtime error:' not in raw
assert [(c['label'], c['exit_code']) for c in r['commands']] == expected
assert all(c['cleanup']['clean_shutdown'] and not c.get('timeout') for c in r['commands'])
for name, digest in r['source_before'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest
for name, digest in r['independent_before'].items():
    if 'independent/' + name in manifest:
        assert hashlib.sha256(read('independent/' + name)).hexdigest() == digest
source = json.loads(read('release-source-sha256.json'))
assert source == json.loads(read('asan-source-sha256.json'))
for name, digest in source.items():
    assert hashlib.sha256(read('build/libslirp-stage/libslirp-v4.6.1/src/' + name)).hexdigest() == digest
lock = json.loads(read('repo/sources.lock.json'))['network_backend']['libslirp_debian']
tar = ROOT / 'repo' / lock['debian_tarball']
assert hashlib.sha256(tar.read_bytes()).hexdigest() == lock['debian_tarball_sha256']
with tarfile.open(tar) as archive:
    assert archive.extractfile('debian/patches/CVE-2026-9539.patch').read() == read(
        'repo/third_party/patches/0002-cve-2026-9539-urgent-data-bounds.patch')
print('PASS: release and ASAN/UBSAN each 108 fixed checks and 29 backend tests;')
print('old-source negative controls each fail 20/102 with six dependent checks skipped;')
print('source/security patch identities unchanged, all 12 process groups empty.')
print('Native evidence only: no Docker or performance acceptance is inferred.')
