#!/usr/bin/env python3
"""Reapply the frozen patch and reparse its real driver-contract regression."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent / 'gpu-mapping-v4/baseline'
REMOTE = '/root/harddind-rebuild/gpu-mapping-owner-v6/'


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def check(ok, message):
    if not ok:
        raise SystemExit(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    for name, digest in json.loads(read('archive-sha256.json')).items():
        check(sha(read(name)) == digest, 'archive changed: ' + name)
    manifest = json.loads(read('manifest.json'))
    for name, digest in manifest.items():
        if name == 'kernel' or name.startswith('reapplied/'):
            continue
        check(sha(read(name)) == digest, 'frozen input changed: ' + name)
    for name, digest in json.loads((BASE / 'manifest.json').read_text()).items():
        check(sha((BASE / name).read_bytes()) == digest, 'baseline changed: ' + name)
    prep = json.loads(read('preparation-status.json'))
    check(prep['status'] == 'finished' and prep['exit_code'] == 0 and
          prep['source_sha256_before'] == prep['source_sha256_after'], 'source changed during rebuild')
    targets = sorted(p for p in (ROOT / 'source').rglob('*') if p.is_file())
    check(len(targets) == 14, 'missing source files')
    with tempfile.TemporaryDirectory(prefix='harddind-mapping-recheck-') as tmp:
        tree = Path(tmp) / 'source'
        shutil.copytree(BASE / 'source', tree)
        patch = subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1'],
                               cwd=tree, input=read('0024-um-gpu-mapping-lifecycle.patch'),
                               capture_output=True)
        check(patch.returncode == 0, 'patch no longer applies exactly')
        for p in targets:
            name = str(p.relative_to(ROOT / 'source'))
            check((tree / name).read_bytes() == p.read_bytes(), 'patch/source mismatch: ' + name)
            check(sha(p.read_bytes()) == prep['source_sha256_before'][name] ==
                  manifest['reapplied/' + name], 'rebuilt source mismatch: ' + name)
    check(b'\\ No newline at end of file' in read('0024-um-gpu-mapping-lifecycle.patch'),
          'missing newline boundary marker')
    old = json.loads(read('rejected-v5/preparation-failure.json'))
    check(old['status'] == 'rejected_preparation' and not old['runtime_started'],
          'rejected v5 counted as a run')
    bad = read('rejected-v5/reapplied/arch/um/kernel/gpu.c')
    check(b'device_initcall(gpu_init);--- a/arch/um/kernel/skas/Makefile' in bad,
          'rejected patch boundary evidence changed')
    report = json.loads(read('runtime/report.json'))
    check(report['sha256'][REMOTE + 'kernel'] == manifest['kernel'] == prep['kernel_sha256'],
          'runtime kernel mismatch')
    prefix = REMOTE + 'run/results/mapping-features-v6/sources/'
    for path, digest in report['sha256'].items():
        if path.startswith(prefix):
            check(sha(read('runtime/sources/' + path[len(prefix):])) == digest,
                  'runtime source changed: ' + path)
    spec = importlib.util.spec_from_file_location('frozen_features', ROOT / 'runtime/sources/gpu_feature_checks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    obs, groups = {}, {str(prep['build_pid'])}
    for name, row in report['phases'].items():
        check(row['exit_code'] == 0 and not row['timed_out'] and row['inputs_unchanged'] and
              row['cleanup']['clean_shutdown'] and not row['cleanup']['forced_cleanup'],
              'unclean phase: ' + name)
        groups.add(str(row['cleanup']['process_group']))
        check(sha(read('runtime/' + name + '/syscalls.strace')) == row['trace_sha256'],
              'trace changed: ' + name)
        if name != 'disabled':
            obs[name] = module.parse(read('runtime/' + name + '/output.log').decode().replace('\r\n', '\n'))
            check(json.loads(json.dumps(obs[name])) == row['feature_observations'],
                  'observation changed: ' + name)
    check(module.compare(obs['native-before'], obs['native-after'])['matches'], 'native controls differ')
    comparison = module.compare(obs['native-before'], obs['guest'])
    check(comparison == report['phases']['guest']['native_comparison'], 'comparison differs')
    cases = {d['case'] for d in comparison['differences']}
    check(comparison['matched'] == 24 and comparison['total'] == 31 and cases == {
        'size_zero', 'size_short', 'size_long', 'size_max', 'null_size_zero',
        'client_zero_bad_size', 'finn_bad_size'}, 'expected seven regressions changed')
    check(report['phases']['guest']['device_ioctl_count'] == 434 and
          report['phases']['guest']['worker_pids'], 'guest worker execution missing')
    check(not report['diagnostic_complete'] and not report['full_acceptance'] and
          not report['internal_cuda_verified'], 'regression counted as acceptance')
    audit = json.loads(read('runtime-cleanup-audit.json'))
    check(audit['all_empty'] and set(audit['groups']) == groups and
          all(v == [[], []] for v in audit['groups'].values()), 'independent cleanup incomplete')
    print(json.dumps({'archive_recheck': 'pass', 'exact_patch_reapply': True,
                      'frozen_sources': len(targets), 'native_contract_matching': '24/31',
                      'regressed_cases': sorted(cases), 'process_groups_empty': len(groups),
                      'allocation_and_cuda': 'not_tested'}, indent=2))


if __name__ == '__main__':
    main()
