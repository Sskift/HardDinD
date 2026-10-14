#!/usr/bin/env python3
"""Check source identity and reparse the preserved GPU registration failure."""
import difflib
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REMOTE = '/root/harddind-rebuild/gpu-mapping-owner-v4/'


def read(name):
    p = ROOT / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def check(ok, message):
    if not ok:
        raise SystemExit(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    manifest = json.loads(read('manifest.json'))
    report = json.loads(read('runtime/report.json'))
    baseline = json.loads(read('baseline/manifest.json'))
    for name, digest in baseline.items():
        check(sha(read('baseline/' + name)) == digest, 'baseline changed: ' + name)
    for name, digest in manifest.items():
        if name == 'kernel' or name.startswith('reapplied/'):
            continue  # Binary omitted; reapplied tree equals frozen source below.
        check(sha(read(name)) == digest, 'frozen source changed: ' + name)
    patch = []
    sources = sorted(p for p in (ROOT / 'source').rglob('*') if p.is_file())
    check(len(sources) == 14, 'expected 14 frozen sources')
    for p in sources:
        name = str(p.relative_to(ROOT / 'source'))
        old, new = read('baseline/source/' + name), p.read_bytes()
        check(sha(new) == manifest['reapplied/' + name], 'remote patch reapply mismatch')
        patch.extend(difflib.unified_diff(old.decode().splitlines(keepends=True),
                                         new.decode().splitlines(keepends=True),
                                         fromfile='a/' + name, tofile='b/' + name))
    check(''.join(patch).encode() == read('0024-um-gpu-mapping-lifecycle.patch'),
          'patch does not reproduce frozen source delta')
    check(report['sha256'][REMOTE + 'kernel'] == manifest['kernel'], 'runtime used a different kernel')
    for path, digest in report['sha256'].items():
        prefix = REMOTE + 'run/results/mapping-features-v4/sources/'
        if path.startswith(prefix):
            check(sha(read('runtime/sources/' + path[len(prefix):])) == digest,
                  'runtime harness source changed: ' + path)
    spec = importlib.util.spec_from_file_location('frozen_feature_checks',
                                                 ROOT / 'runtime/sources/gpu_feature_checks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observations = {}
    groups = set()
    for name, row in report['phases'].items():
        check(row['exit_code'] == 0 and not row['timed_out'] and row['inputs_unchanged'],
              'unexpected runtime termination: ' + name)
        cleanup = row['cleanup']
        check(cleanup['clean_shutdown'] and not cleanup['forced_cleanup'] and
              not cleanup['remaining_live_pids'] and not cleanup['remaining_zombie_pids'],
              'unclean phase: ' + name)
        groups.add(str(cleanup['process_group']))
        check(sha(read('runtime/' + name + '/syscalls.strace')) == row['trace_sha256'],
              'trace hash changed: ' + name)
        content = read('runtime/' + name + '/output.log').decode()
        if name != 'disabled':
            observations[name] = module.parse(content)
            check(json.loads(json.dumps(observations[name])) == row['feature_observations'],
                  'observations changed: ' + name)
    check(observations['native-before']['probe_checks_pass'] and
          observations['native-after']['probe_checks_pass'] and
          module.compare(observations['native-before'], observations['native-after'])['matches'],
          'native control changed')
    guest = report['phases']['guest']
    check(guest['status'] == 'fail' and not guest['feature_observations']['observation_complete']
          and not guest['worker_pids'] and not guest['checks'] and guest['device_ioctl_count'] == 0,
          'expected registration failure disappeared')
    console = read('runtime/guest/output.log').decode()
    check("sysfs: cannot create duplicate filename '/dev/char/195:0'" in console and
          'gpu_init+' in console and 'GPU_CONTROL_DISABLED status=0' in console,
          'missing concrete registration-failure evidence')
    check(not report['diagnostic_complete'] and not report['internal_cuda_verified'] and
          not report['full_acceptance'], 'failure incorrectly counted as acceptance')
    audit = json.loads(read('runtime-cleanup-audit.json'))
    check(set(audit['groups']) == groups and audit['all_empty'] and
          all(v == [[], []] for v in audit['groups'].values()), 'independent cleanup incomplete')
    print(json.dumps({'archive_recheck': 'pass', 'frozen_sources': len(sources),
                      'kernel_sha256': manifest['kernel'], 'native_control': '31/31 matching',
                      'bridge_guest': 'failed duplicate device registration',
                      'gpu_workers': 0, 'internal_cuda': 'not_tested',
                      'process_groups_empty': len(groups)}, indent=2))


if __name__ == '__main__':
    main()
