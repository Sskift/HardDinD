#!/usr/bin/env python3
"""Reparse the actual failed four-card native run; never execute GPU code."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def check(ok, message):
    if not ok:
        raise SystemExit(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    manifest = json.loads(read('remote-files-sha256.json'))
    for name, digest in manifest.items():
        check(sha(read(name)) == digest, 'archived file changed: ' + name)
    report = json.loads(read('owner-report.json'))
    for name, digest in report['source_sha256'].items():
        if '__pycache__' in name:
            continue  # Bytecode omitted; the source and runtime audit are preserved.
        check(sha(read(name)) == digest, 'runtime source changed: ' + name)
    for name, digest in json.loads(read('author-v2/source-manifest.json')).items():
        check(sha(read('author-v2/' + name)) == digest, 'author baseline changed: ' + name)
    check(read('author-v2/source-manifest.json') == read('author-v2-source-manifest.json'),
          'original protocol source manifest mismatch')
    before = [json.loads(s) for s in read('original-protocol.stdout').splitlines()]
    after = [json.loads(s) for s in read('fixed-protocol.stdout').splitlines()]
    check(before[0]['bytes'] == 163 and before[0]['checksum'] == 429496 and
          not before[0]['preserved'] and after[0]['checksum'] == 4294967295 and
          after[0]['preserved'], 'truncation regression changed')
    for field in ['unsigned_overflow_rc', 'signed_overflow_rc', 'trailing_field_rc']:
        check(before[1][field] == 0 and after[1][field] == -1, 'bounds regression changed: ' + field)
    commands = {c['label']: c for c in report['commands']}
    check(len(commands) == 10 and commands['native']['exit_code'] == 1 and
          report['exit_code'] == 1 and report['status'] == 'finished', 'native failure changed')
    for name, command in commands.items():
        check(command['exit_code'] == (1 if name == 'native' else 0) and
              not command['timed_out'] and command['cleanup']['clean_shutdown'] and
              not command['cleanup']['forced_cleanup'], 'command status changed: ' + name)
    check(b'Ran 50 tests' in read('checker-tests.stderr') and
          read('checker-tests.stderr').rstrip().endswith(b'OK (skipped=2)'), 'checker test result changed')
    text = read('native.stdout').decode()
    rows = [json.loads(s) for s in text.splitlines()]
    shared = [r for r in rows if r.get('phase') == 'shared' and r.get('type') == 'worker']
    children = [r for r in rows if r.get('phase') == 'child' and r.get('type') == 'worker']
    check(len(shared) == len(children) == 4, 'missing native worker records')
    check(all(r['attach_rc'] == 201 for r in shared), 'shared attach failure changed')
    check(all(not r['received'] and r['child_exit'] == 0 and r['allocs'] == r['frees'] == 6
              and r['mismatches'] == 0 and all(c['start_ns'] == c['end_ns'] == 0
              for c in r['cycles_detail']) for r in children), 'child protocol failure changed')
    spec = importlib.util.spec_from_file_location('frozen_checks', ROOT / 'scripts/gpu_lifecycle_checks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    uuids = [s.split(',')[0].strip() for s in read('inventory-before.stdout').decode().splitlines()]
    verdict = module.analyze(text, {'uuids': uuids})
    check(verdict == json.loads(read('owner-failure-reanalysis.json')), 'frozen checker reparse differs')
    check(not verdict['ok'] and verdict['overlap_ns']['child'] == -74022055,
          'failed concurrency evidence changed')
    audit = json.loads(read('owner-cleanup-audit.json'))
    check(audit['all_empty'] and set(audit['groups']) == {str(c['pid']) for c in commands.values()}
          and all(v == [[], []] for v in audit['groups'].values()), 'independent cleanup incomplete')
    inputs = json.loads(read('owner-input-audit.json'))
    check(inputs['original_report_sha256'] == sha(read('owner-report.json')) and
          inputs['inputs_unchanged'] and all(inputs['source_matches'].values()) and
          all(inputs['library_matches'].values()) and inputs['probe_binary_matches'], 'input audit failed')
    check(not report['full_acceptance'] and not report['performance_acceptance'] and
          report['internal_gpu_acceptance'] == 'not_tested', 'failure counted as acceptance')
    print(json.dumps({'archive_recheck': 'pass', 'native_lifecycle': 'fail',
                      'protocol_truncation_and_bounds_regressions': 'pass',
                      'checker_tests': {'pass': 48, 'skip': 2}, 'process_groups_empty': 10,
                      'internal_gpu': 'not_tested', 'performance_acceptance': False}, indent=2))


if __name__ == '__main__':
    main()
