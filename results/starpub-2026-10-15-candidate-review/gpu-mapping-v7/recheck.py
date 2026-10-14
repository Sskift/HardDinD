#!/usr/bin/env python3
"""Verify the v7 control repair and v7/v8/v9 CUDA node-selection evidence."""
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


def read(root, name):
    p = root / name
    return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def check(ok, message):
    if not ok:
        raise SystemExit(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(root, name):
    return json.loads(read(root, name))


def archive(root):
    for name, digest in load(root, 'archive-sha256.json').items():
        check(sha(read(root, name)) == digest, 'archive changed: ' + str(root / name))


def groups_in(value):
    groups = set()
    if isinstance(value, dict):
        if 'process_group' in value and 'clean_shutdown' in value:
            check(value['clean_shutdown'] and not value['forced_cleanup'] and
                  not value['remaining_live_pids'] and not value['remaining_zombie_pids'],
                  'unclean retained process group')
            groups.add(str(value['process_group']))
        for child in value.values():
            groups.update(groups_in(child))
    elif isinstance(value, list):
        for child in value:
            groups.update(groups_in(child))
    return groups


def audit(root, groups):
    data = load(root, 'runtime-cleanup-audit.json')
    check(data['all_empty'] and set(data['groups']) == groups and
          all(v == [[], []] for v in data['groups'].values()), 'independent cleanup incomplete')


def control(root, name, module_name, key, count):
    folder = root / 'runtime' / name
    report = load(folder, 'report.json')
    spec = importlib.util.spec_from_file_location(module_name, folder / 'sources' / (module_name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    obs = {}
    prefix = '/root/harddind-rebuild/gpu-mapping-owner-v7/run/results/' + name + '/sources/'
    for path, digest in report['sha256'].items():
        if path.startswith(prefix):
            check(sha(read(folder, 'sources/' + path[len(prefix):])) == digest, 'control source changed')
    for phase, row in report['phases'].items():
        check(row['status'] == 'pass' and row['exit_code'] == 0 and not row['timed_out'] and
              row['inputs_unchanged'], 'control phase failed: ' + phase)
        check(sha(read(folder, phase + '/syscalls.strace')) == row['trace_sha256'], 'control trace changed')
        if phase != 'disabled':
            content = read(folder, phase + '/output.log').decode().replace('\r\n', '\n')
            obs[phase] = module.parse(content)
            check(json.loads(json.dumps(obs[phase])) == row[key], 'control reparse differs')
    for phase in ['guest', 'native-after']:
        comparison = module.compare(obs['native-before'], obs[phase])
        check(comparison == report['phases'][phase]['native_comparison'] and
              comparison['matches'] and comparison['matched'] == comparison['total'] == count,
              'control contract differs')
    check(report['diagnostic_complete'] and not report['full_acceptance'] and
          not report['internal_cuda_verified'], 'control acceptance boundary changed')
    return report


def cuda(root, version, kernel):
    folder = root / 'runtime' / ('mapping-cuda-v' + str(version))
    report = load(folder, 'report.json')
    prefix = '/root/harddind-rebuild/gpu-mapping-owner-v' + str(version) + '/'
    check(report['sha256'][prefix + 'kernel'] == kernel, 'CUDA kernel differs')
    for path, digest in report['sha256'].items():
        if '/repo/scripts/' in path or '/repo/src/' in path:
            check(sha(read(folder, 'sources/' + Path(path).name)) == digest, 'CUDA source differs')
    check(report['normal_shutdown'] and report['inputs_unchanged'] and report['libraries_unchanged'] and
          not report.get('error') and not report.get('cleanup_error') and not report['internal_cuda_verified'],
          'CUDA input/cleanup/result boundary differs')
    for label, row in report['phases'].items():
        check(not row['timed_out'] and row['exit_code'] == (1 if label == 'docker' else 0), 'CUDA phase exit differs')
        parsed = [json.loads(line) for line in read(folder, label + '.stdout').decode().splitlines()
                  if line.startswith('{"gpu":')]
        check(parsed == row['compute_results'], 'compute output differs')
        if label != 'docker':
            check(len(parsed) == 4 and {r['gpu'] for r in parsed} == set(range(4)) and
                  all(r['verified'] is True and r['elements'] == 1 << 20 for r in parsed) and
                  row['all_gpus_verified'], 'native compute control failed')
    trace = read(folder, 'docker.strace')
    check(sha(trace) == report['phases']['docker']['trace_sha256'], 'CUDA trace changed')
    if version == 7:
        check(not report['diagnostic_complete'] and report['uvm_node_available'] and
              b'st_rdev=makedev(0xfc, 0xff)' in trace and b'makedev(0xc3, 0xff)' in trace and
              b'EPERM (Operation not permitted)' in trace, 'alias device-number failure changed')
    elif version == 8:
        check(report['diagnostic_complete'] and report['device_source_prefix'] == '/dev/nvidia' and
              report['uvm_device_source'] == '/dev/nvidia-uvm', 'standard node selection failed')
        errors = []
        for line in read(folder, 'docker.stderr').decode().splitlines():
            if line.startswith('CUDA_IOCTL ') and ' rc=-1 ' in line:
                fields = dict(tok.split('=', 1) for tok in line.split() if '=' in tok)
                errors.append((fields['request'], fields.get('rm_command'), fields.get('params_size'), fields['errno']))
        check(errors.count(('0xc23046d7', None, None, '95')) == 4 and
              errors.count(('0xc020462a', '0x216', '128', '95')) == 4 and len(errors) == 8,
              'unsupported operations changed')
    elif version == 9:
        check(report['device_source_prefix'] == '/dev/harddind-nvidia' and
              report['device_node_probes'][0]['label'] == 'gpu-nodes-standard' and
              report['device_node_probes'][0]['exit_code'] != 0 and
              report['device_node_probes'][1]['label'] == 'gpu-nodes-compat' and
              report['device_node_probes'][1]['exit_code'] == 0 and not report['uvm_node_available'],
              'old-backend fallback behavior changed')
    return report


def main():
    archive(ROOT)
    prep = load(ROOT, 'preparation-status.json')
    manifest = load(ROOT, 'manifest.json')
    check(prep['status'] == 'finished' and prep['exit_code'] == 0 and
          prep['source_sha256_before'] == prep['source_sha256_after'], 'rebuild inputs changed')
    for name, digest in manifest.items():
        if name != 'kernel' and not name.startswith('reapplied/'):
            check(sha(read(ROOT, name)) == digest, 'frozen source changed: ' + name)
    for name, digest in json.loads((BASE / 'manifest.json').read_text()).items():
        check(sha((BASE / name).read_bytes()) == digest, 'baseline changed: ' + name)
    with tempfile.TemporaryDirectory(prefix='harddind-mapping-v7-') as tmp:
        tree = Path(tmp) / 'source'
        shutil.copytree(BASE / 'source', tree)
        patch = subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1'], cwd=tree,
                               input=read(ROOT, '0024-um-gpu-mapping-lifecycle.patch'), capture_output=True)
        check(patch.returncode == 0, 'patch failed to reapply')
        for p in (ROOT / 'source').rglob('*'):
            if p.is_file():
                name = str(p.relative_to(ROOT / 'source'))
                check(p.read_bytes() == (tree / name).read_bytes() and sha(p.read_bytes()) ==
                      prep['source_sha256_before'][name] == manifest['reapplied/' + name], 'source mismatch: ' + name)
    kernel = manifest['kernel']
    check(kernel == prep['kernel_sha256'], 'rebuild kernel mismatch')
    reports = [control(ROOT, 'mapping-features-v7', 'gpu_feature_checks', 'feature_observations', 31),
               control(ROOT, 'mapping-alloc-v7', 'gpu_alloc_checks', 'alloc_observations', 74),
               cuda(ROOT, 7, kernel)]
    audit(ROOT, groups_in([prep] + reports))
    for version in [8, 9]:
        root = ROOT.parent / ('gpu-mapping-v' + str(version))
        archive(root)
        expected = kernel if version == 8 else '9192f196143242aab3dfacc2966d7bcc605db9cc0caadfe68b09a76cb42b557e'
        report = cuda(root, version, expected)
        audit(root, groups_in(report))
    check(read(ROOT.parent / 'gpu-mapping-v8', 'runtime/mapping-cuda-v8/sources/run-gpu-cuda.py') ==
          read(ROOT.parent / 'gpu-mapping-v9', 'runtime/mapping-cuda-v9/sources/run-gpu-cuda.py'),
          'standard/fallback tests used different harnesses')
    print(json.dumps({'archive_recheck': 'pass', 'feature_contract': '31/31', 'allocation_contract': '74/74',
                      'standard_nodes': 'verified', 'legacy_node_fallback': 'verified',
                      'native_before_after': 'four-card computation verified in all three runs',
                      'internal_cuda': 'still failed; typed ioctl coverage remains incomplete'}, indent=2))


if __name__ == '__main__':
    main()
