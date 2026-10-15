from pathlib import Path
import gzip, hashlib, importlib.util, json, sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent

def read(name):
    p = root / name
    return p.read_bytes() if p.is_file() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())

manifest = json.loads(read('archive-sha256.json'))
for name, digest in manifest.items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
status = json.loads(read('status.json'))
audit = json.loads(read('post-run-audit.json'))
assert status['status'] == 'finished' and status['exit_code'] == 0 and status['input_unchanged']
assert status['locked_wall_seconds'] <= 60
assert not status['full_acceptance'] and not status['performance_acceptance']
assert status['guest_verification'] == 'not_tested'
assert audit['groups_empty'] and audit['inputs_unchanged']
assert len(audit['groups']) == 4 and all(v == [[], []] for v in audit['groups'].values())
assert all(c['exit_code'] == 0 and not c.get('timeout') and c['cleanup']['clean_shutdown'] for c in status['commands'])
for name, digest in status['inputs'].items():
    assert hashlib.sha256(read(name)).hexdigest() == digest
assert json.loads(read('selftest.stdout'))['status'] == 'pass'
unit = read('unit-tests.stderr').decode()
assert 'Ran 75 tests' in unit and unit.rstrip().endswith('OK') and 'skipped' not in unit
spec = importlib.util.spec_from_file_location('frozen_cpu_checks', root / 'scripts/cpu_limit_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
events = checks.parse_events(read('native-spin.stdout').decode())
summary = checks.spin_summary(events)
assert summary['completed_work'] == status['completed_work']
assert summary['completed_work']['status'] == 'pass' and summary['completed_work']['total_blocks'] > 0
assert events[0]['threads'] == len(events[-1]['workers']) == 4
print('PASS: 75 Linux tests, real four-thread native completed-work checksums, unchanged inputs and four empty groups.')
print('Guest use and native-equivalent Docker performance remain untested for this probe revision.')
