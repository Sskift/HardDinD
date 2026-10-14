#!/usr/bin/env python3
"""Recheck frozen CPU-share, POSIX-lock and native CUDA failures offline.

This does not execute a guest, GPU, workload or compiler. CPU shares v3/v4
did not save their complete evaluate() input; do not pretend to replay it.
"""
from collections import Counter
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def check(value, message):
    if not value:
        raise SystemExit(message)


def read(root, name):
    path = root / name
    if path.exists():
        return path.read_bytes()
    return gzip.decompress(path.with_name(path.name + '.gz').read_bytes())


def load(root, name):
    return json.loads(read(root, name))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def groups_in(value):
    groups = set()
    if isinstance(value, dict):
        if 'process_group' in value and 'clean_shutdown' in value:
            check(value['clean_shutdown'] and not value['forced_cleanup'] and
                  not value['remaining_live_pids'] and not value['remaining_zombie_pids'],
                  'recorded group did not cleanly exit')
            groups.add(str(value['process_group']))
        for child in value.values():
            groups.update(groups_in(child))
    elif isinstance(value, list):
        for child in value:
            groups.update(groups_in(child))
    return groups


def verify_archive(root, owner):
    for name, digest in load(root, 'archive-sha256.json').items():
        check(sha(read(root, name)) == digest, 'archive changed: ' + str(root / name))
    for name, digest in owner.get('sources_before', {}).items():
        check(sha(read(root, 'repo/' + name)) == digest, 'repo source changed: ' + name)
    for name, digest in owner.get('source_sha256', {}).items():
        check(sha(read(root, name)) == digest, 'probe source changed: ' + name)
    check(owner['status'] == 'finished' and owner['inputs_unchanged'] and
          not owner['full_acceptance'] and not owner['performance_acceptance'],
          'owner completion/input/acceptance boundary changed')


def verify_cleanup(root, owner, report, count):
    audit = load(root, 'owner-cleanup-audit.json')
    check(audit['all_empty'] and len(audit['groups']) == count and
          all(value == [[], []] for value in audit['groups'].values()),
          'independent cleanup evidence differs')
    check(set(audit['groups']) == groups_in(owner) | groups_in(report),
          'independent cleanup omitted a recorded group')


for version in (3, 4):
    root = ROOT / ('cpu-shares-v' + str(version))
    owner = load(root, 'owner-status.json')
    report = load(root, 'runtime/shares-v' + str(version) + '/report.json')
    verify_archive(root, owner)
    verify_cleanup(root, owner, report, 48 if version == 3 else 51)
    check(not report['full_acceptance'] and not report['performance_acceptance'] and
          report['evidence'] == {}, 'CPU evidence boundary changed')
    checks = module(root / 'repo/scripts/cpu_share_checks.py', 'shares_v' + str(version))
    for container in ('a', 'b'):
        content = read(root, 'runtime/shares-v%d/logs-%s.stdout' %
                       (version, container)).decode()
        lines = content.splitlines()
        invalid = []
        for i, line in enumerate(lines):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                invalid.append(i)
        if version == 3:
            check(invalid == [0, len(lines) - 1], 'v3 malformed endpoints not reproduced')
        else:
            check(not invalid, 'v4 real stream is not JSON')
            checks.validate_contend_stream(checks.parse_events(content), 2)
    if version == 3:
        check(report['verdict'] == 'fail' and
              report['checks']['probe_streams']['status'] == 'fail', 'v3 failure disappeared')
    else:
        check(report['verdict'] == 'indeterminate' and
              Counter(v['status'] for v in report['checks'].values()) ==
              Counter({'pass': 14, 'indeterminate': 2, 'not_tested': 1}),
              'v4 result boundary changed')
        proof = load(root, 'endpoint-regression.json')
        check(len(proof['original-contend']['invalid']) == 2 and
              not proof['fixed-contend']['invalid'], 'actual endpoint regression missing')
        check(report['checks']['phase_2to1']['status'] == 'pass' and
              report['checks']['phase_reversed_1to2']['status'] == 'indeterminate' and
              report['checks']['phase_equal_control']['status'] == 'indeterminate',
              'incomplete phases claimed as passed')

root = ROOT / 'posix-v5'
owner = load(root, 'owner-status.json')
report = load(root, 'runtime/posix-v5.json')
verify_archive(root, owner)
verify_cleanup(root, owner, report, 135)
check(Counter(report['checks']['native'].values()) == {'pass': 46} and
      Counter(report['checks']['docker'].values()) == {'pass': 45, 'fail': 1} and
      report['checks']['docker']['posix_exec_preserves'] == 'fail' and
      report['functional_complete'] and not report['all_checks_passed'] and
      not report['benchmark_complete'] and not report['full_acceptance'],
      'POSIX result boundary changed')
raw = read(root, 'runtime.log').decode()
for mode, cases in report['checks'].items():
    for case, status in cases.items():
        check('%s %s %s' % (mode, case, status) in raw,
              'missing raw POSIX case: ' + case)
check(report['runtime']['phase'] == 'stopped' and
      not report['runtime']['forced_shutdown'], 'POSIX shutdown differs')

root = ROOT / 'posix-v6'
owner = load(root, 'owner-status.json')
report = load(root, 'runtime/posix-v6.json')
verify_archive(root, owner)
verify_cleanup(root, owner, report, 133)
check(owner['exit_code'] == 0 and report['functional_complete'] and
      report['all_checks_passed'] and not report['benchmark_complete'] and
      not report['full_acceptance'] and report['runtime']['phase'] == 'stopped' and
      not report['runtime']['forced_shutdown'], 'POSIX v6 boundary changed')
raw = read(root, 'runtime.log').decode()
for mode, cases in report['checks'].items():
    check(Counter(cases.values()) == {'pass': 47}, 'POSIX v6 coverage differs')
    for case, status in cases.items():
        check('%s %s %s' % (mode, case, status) in raw,
              'missing raw POSIX v6 case: ' + case)
check(owner['binary_sha256'] == load(ROOT / 'posix-v5', 'owner-status.json')['binary_sha256'],
      'POSIX backend changed between fixture comparison runs')
integration = load(root, 'integration/report.json')
check(integration['status'] == 'finished' and integration['exit_code'] == 0 and
      integration['groups_empty'] and integration['inputs_unchanged'], 'POSIX integration failed')
check('Ran 176 tests' in read(root, 'integration/check.stderr').decode() and
      'skipped=' not in read(root, 'integration/check.stderr').decode(), 'POSIX integration incomplete')
binding = load(root, 'integration/source-binding.json')
check(sha(read(root, 'integration/main-sources.lock.json')) == binding['main_sources_lock_sha256'],
      'integrated dependency lock changed')
main_lock = load(root, 'integration/main-sources.lock.json')
old_lock = load(root, 'repo/sources.lock.json')
check({k: v for k, v in main_lock.items() if k != 'nvidia_abi_reference'} ==
      {k: v for k, v in old_lock.items() if k != 'nvidia_abi_reference'},
      'non-GPU pinned dependencies differ from tested backend')
for name, digest in binding['patches_sha256'].items():
    check(sha(read(root, 'repo/' + name)) == digest, 'integrated virtiofs patch differs')
for name in ('src/share_probe.c', 'scripts/run-shared-locks.py', 'scripts/harddind.py'):
    check(integration['sha256'][name] == owner['sources_before'][name],
          'integrated POSIX runtime differs from actual tested source')

root = ROOT / 'gpu-lifecycle-v4'
owner = load(root, 'owner-report.json')
verify_archive(root, owner)
audit = load(root, 'owner-cleanup-audit.json')
check(set(audit) == groups_in(owner) and len(audit) == 13 and
      all(value == [[], []] for value in audit.values()) and owner['groups_empty'] and
      owner['libraries_unchanged'] and owner['inventory_unchanged'], 'CUDA cleanup/input mismatch')
old = [json.loads(line) for line in read(root, 'original-shared-json.stdout').splitlines()]
new = [json.loads(line) for line in read(root, 'shared-json.stdout').splitlines()]
check([type(row['current_match']) for row in old] == [int, int] and
      [type(row['current_match']) for row in new] == [bool, bool] and
      [row['current_match'] for row in new] == [True, False], 'real bool serializer regression missing')
uuids = [line.split(',')[0].strip() for line in
         read(root, 'inventory-before.stdout').decode().splitlines()]
checks = module(root / 'scripts/gpu_lifecycle_checks.py', 'gpu_lifecycle_v4')
verdict = checks.analyze(read(root, 'native.stdout').decode(), {'uuids': uuids})
check(verdict == owner['native_verdict'] == load(root, 'native-verdict.json') and
      not verdict['ok'] and owner['internal_gpu_acceptance'] == 'not_tested' and
      next(c for c in owner['commands'] if c['label'] == 'native')['exit_code'] == -13,
      'native failure reparse differs')
root = ROOT / 'gpu-lifecycle-v5'
owner = load(root, 'owner-report.json')
verify_archive(root, owner)
audit = load(root, 'owner-cleanup-audit.json')
check(set(audit) == groups_in(owner) and len(audit) == 11 and
      all(value == [[], []] for value in audit.values()) and owner['groups_empty'] and
      owner['libraries_unchanged'] and owner['inventory_unchanged'], 'v5 cleanup/input mismatch')
uuids = [line.split(',')[0].strip() for line in
         read(root, 'inventory-before.stdout').decode().splitlines()]
checks = module(root / 'scripts/gpu_lifecycle_checks.py', 'gpu_lifecycle_v5')
verdict = checks.analyze(read(root, 'native.stdout').decode(), {'uuids': uuids})
check(verdict == owner['native_verdict'] == load(root, 'native-verdict.json') and
      verdict['ok'] and verdict['gpu_count'] == 4 and
      owner['internal_gpu_acceptance'] == 'not_tested' and owner['exit_code'] == 0 and
      all(c['exit_code'] == 0 and not c['timed_out'] for c in owner['commands']),
      'v5 native result boundary changed')
integration = load(root, 'integration/report.json')
check(integration['status'] == 'passed' and
      all(c['exit_code'] == 0 for c in integration['commands']), 'lifecycle integration failed')
for name in ('src/cuda_lifecycle_probe.c', 'scripts/gpu_lifecycle_checks.py'):
    check(integration['source_sha256'][name] == owner['source_sha256'][name],
          'integrated lifecycle implementation differs from native-tested source')
check('Ran 66 tests' in read(root, 'checker-tests.stderr').decode() and
      'skipped=' not in read(root, 'checker-tests.stderr').decode(), 'v5 tests incomplete')
root = ROOT / 'fp-matrix-v1'
owner = load(root, 'owner-status.json')
verify_archive(root, owner)
for name, digest in owner['sources'].items():
    check(sha(read(root, 'repo/' + name)) == digest, 'FP source changed: ' + name)
expected_cases = {
    'gp-syscall', 'fp-preserve', 'fp-signal-preserve', 'fp-signal-edit',
    'gp-signal-edit', 'fp-nested', 'fp-threads-1', 'fp-threads-4',
    'frame-churn', 'thread-exit-churn', 'runner-lifetime', 'fork-fp',
    'exec-reset', 'ptrace-prefix-semantics', 'ptrace-gp-fp',
}
expected_cells = {
    f'{kernel}-{cpus}cpu-p{parallel}-l{lazy}'
    for kernel in ('eager', 'lazy') for cpus in (1, 4) for parallel in (0, 1)
    for lazy in ((0,) if kernel == 'eager' else (0, 1))
}
check(set(owner['cells']) == expected_cells and owner['all_cells_passed'] and
      owner['exit_code'] == 0 and owner['classifier_negative_checks'] == 6,
      'FP matrix coverage or completion changed')
for label, verdict in {'native': owner['native'], **owner['cells']}.items():
    content = read(root, label + '.stdout').decode()
    rows = [json.loads(line) for line in content.splitlines() if line.startswith('{')]
    check(Counter(row.get('case') for row in rows) == Counter(expected_cases) and
          rows == verdict['rows'] and verdict['verdict'] == 'pass' and
          not verdict['errors'] and not verdict['skipped'] and
          all(row['test'] == 'fp_probe' and row['status'] == 'pass' and
              row['ok'] is True for row in rows), 'FP raw case mismatch: ' + label)
    if label != 'native':
        for prefix, expected in (
                ('HARDDIND_FP_PROBE_SHA256=', 'HARDDIND_FP_PROBE_SHA256=' + owner['probe_sha256']),
                ('HARDDIND_FP_PROBE_RC=', 'HARDDIND_FP_PROBE_RC=0'),
                ('HARDDIND_FP_END ', 'HARDDIND_FP_END status=0')):
            check([line for line in content.splitlines() if line.startswith(prefix)] == [expected],
                  'FP completion/hash marker differs: ' + label)
        check(verdict['kernel_unchanged'], 'FP kernel changed')
audit = load(root, 'owner-cleanup-audit.json')
check(len(owner['commands']) == len(audit) == 15 and owner['groups_empty'] and
      set(audit) == groups_in(owner) and all(v == [[], []] for v in audit.values()) and
      all(c['exit_code'] == 0 and not c['timed_out'] for c in owner['commands']),
      'FP command or independent cleanup evidence changed')
print('Archive/source hashes, raw failures, CUDA/FP reparse and recorded cleanup groups verified.')
print('CPU v3 fail; CPU v4 indeterminate; POSIX v5 fail, v6 pass; native CUDA v4 fail, v5 pass.')
print('FP matrix: native 15 cases and 12 guest configurations x 15 cases pass; diagnostic only.')
print('Internal GPU and full/performance acceptance remain unverified.')
