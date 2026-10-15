#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('owner-status.json'))
assert r['status'] == 'finished' and r['exit_code'] == 1 and r['groups_empty']
assert len(r['groups']) == 3 and all(value == [[], []] for value in r['groups'].values())
assert [(c['label'], c['exit_code']) for c in r['commands']] == [
    ('release-build', 0), ('release-independent-build', 0), ('release-independent-tests', 1)]
assert all(c['cleanup']['clean_shutdown'] and not c.get('timeout') for c in r['commands'])
audit = json.loads(read('post-failure-audit.json'))
assert audit['source_unchanged'] and audit['reviewer_unchanged']
assert audit['source_after_failure'] == r['source_before']
assert audit['reviewer_after_failure'] == r['independent_before']
for name, digest in r['source_before'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest
for name, digest in r['independent_before'].items():
    assert hashlib.sha256(read('independent/tests/' + name)).hexdigest() == digest
source = json.loads(read('release-source-sha256.json'))
for name, digest in source.items():
    assert hashlib.sha256(read('build/libslirp-stage/libslirp-v4.6.1/src/' + name)).hexdigest() == digest
assert b'NATIVE IP6 FRAG TESTS: ALL PASS' in read('release-build.stdout')
raw = read('release-independent-tests.stdout')
assert raw.count(b'  FAIL [') == 4
assert b'SUMMARY checks=75 failures=4 bug_conditions_observed=0' in raw
lock = json.loads(read('repo/sources.lock.json'))['network_backend']['libslirp_debian']
tar = ROOT / 'repo' / lock['debian_tarball']
assert hashlib.sha256(tar.read_bytes()).hexdigest() == lock['debian_tarball_sha256']
with tarfile.open(tar) as archive:
    assert archive.extractfile('debian/patches/CVE-2026-9539.patch').read() == read(
        'repo/third_party/patches/0002-cve-2026-9539-urgent-data-bounds.patch')
print('PASS: final source/security patch identities, failed independent 4/75 run and 3 empty groups preserved.')
print('No ASan, backend flow or Docker pass is claimed for this stopped run.')
