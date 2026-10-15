from pathlib import Path
import copy, gzip, hashlib, importlib.util, json, sys

sys.dont_write_bytecode = True
b = Path(__file__).resolve().parent

def read(name):
    p = b / name
    return p.read_bytes() if p.is_file() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())

for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
r = json.loads(read('report.json'))
assert r['exit_code'] == 0 and not r['full_acceptance'] and not r['performance_acceptance']
for name, digest in r['inputs'].items():
    assert hashlib.sha256(read(name)).hexdigest() == digest
assert hashlib.sha256(read('native-spin.stdout')).hexdigest() == r['raw_sha256']
text = read('unit.stderr').decode()
assert 'Ran 5 tests' in text and text.rstrip().endswith('OK') and 'skipped' not in text
spec = importlib.util.spec_from_file_location('period_checks', b / 'scripts/cpu_limit_checks.py')
c = importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
events = c.parse_events(read('native-spin.stdout').decode())
assert c.spin_summary(events)['completed_work'] == r['original_work']
bad = copy.deepcopy(events[-1]['workers'])
bad[0]['work_blocks'] += 1 << 55
try:
    c.completed_work(events[0], bad, 1)
except ValueError as error:
    assert str(error) == r['period_alias_rejected']
else:
    raise AssertionError('period alias passed')
print('PASS: original work counters unchanged; exact-period alias rejected; five targeted tests passed.')
