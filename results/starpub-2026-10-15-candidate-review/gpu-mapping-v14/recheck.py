#!/usr/bin/env python3
"""Reapply the frozen v14 patch and reparse its actual control/CUDA evidence."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('mapping_checks', ROOT.parent / 'gpu-mapping-v7/recheck.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
c.archive(ROOT)
prep = c.load(ROOT, 'preparation-status.json')
manifest = c.load(ROOT, 'manifest.json')
c.check(prep['status'] == 'finished' and prep['exit_code'] == 0 and
        prep['source_sha256_before'] == prep['source_sha256_after'], 'source changed during build')
for name, digest in manifest.items():
    if name != 'kernel' and not name.startswith('reapplied/'):
        c.check(c.sha(c.read(ROOT, name)) == digest, 'frozen input changed: ' + name)
for name, digest in c.load(c.BASE, 'manifest.json').items():
    c.check(c.sha(c.read(c.BASE, name)) == digest, 'baseline changed: ' + name)
with tempfile.TemporaryDirectory(prefix='harddind-mapping-v14-') as tmp:
    tree = Path(tmp) / 'source'
    shutil.copytree(c.BASE / 'source', tree)
    patch = subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1'], cwd=tree,
                           input=c.read(ROOT, '0024-um-gpu-mapping-lifecycle.patch'), capture_output=True)
    c.check(patch.returncode == 0, 'patch does not reapply')
    for path in (ROOT / 'source').rglob('*'):
        if path.is_file():
            name = str(path.relative_to(ROOT / 'source'))
            c.check(path.read_bytes() == (tree / name).read_bytes() and
                    c.sha(path.read_bytes()) == prep['source_sha256_before'][name] ==
                    manifest['reapplied/' + name], 'reapplied source differs: ' + name)
kernel = manifest['kernel']
c.check(kernel == prep['kernel_sha256'] ==
        '9306f4e6045bb26821338c6962f8c67f887ea6f86f917524ebe4b22a25a25927',
        'kernel binding differs')
reports = [c.control(ROOT, 'mapping-features-v14', 'gpu_feature_checks', 'feature_observations', 31),
           c.control(ROOT, 'mapping-alloc-v14', 'gpu_alloc_checks', 'alloc_observations', 74)]
folder = ROOT / 'runtime/mapping-cuda-v14'
cuda = c.load(folder, 'report.json')
reports.append(cuda)
c.check(cuda['sha256']['/root/harddind-rebuild/gpu-mapping-owner-v14/kernel'] == kernel,
        'CUDA kernel binding differs')
for path, digest in cuda['sha256'].items():
    if '/repo/scripts/' in path or '/repo/src/' in path:
        c.check(c.sha(c.read(folder, 'sources/' + Path(path).name)) == digest, 'CUDA source differs')
c.check(cuda['normal_shutdown'] and cuda['inputs_unchanged'] and cuda['libraries_unchanged'] and
        cuda['internal_cuda_verified'] and cuda['diagnostic_complete'] and
        not cuda.get('error') and not cuda.get('cleanup_error'), 'CUDA result/cleanup differs')
c.check(cuda['device_source_prefix'] == '/dev/nvidia' and
        cuda['uvm_device_source'] == '/dev/nvidia-uvm' and cuda['async_wait_verified'],
        'device or async path differs')
for label, phase in cuda['phases'].items():
    c.check(phase['exit_code'] == 0 and not phase['timed_out'] and phase['cleanup']['clean_shutdown'],
            'CUDA phase failed: ' + label)
    compute = [json.loads(line) for line in c.read(folder, label + '.stdout').decode().splitlines()]
    c.check(compute == phase['compute_results'] and len(compute) == 4 and
            {row['gpu'] for row in compute} == {0, 1, 2, 3} and
            all(row['verified'] is True and row['elements'] == 1 << 20 for row in compute) and
            phase['all_gpus_verified'], 'four-card calculation failed: ' + label)
    rows = [dict(token.split('=', 1) for token in line.split() if '=' in token)
            for line in c.read(folder, label + '.stderr').decode().splitlines()
            if line.startswith('CUDA_IOCTL ')]
    for command, size in [('0x20808159', 332), ('0x20808162', 1), ('0x20810108', 992)]:
        selected = [row for row in rows if row.get('rm_command') == command]
        c.check(len(selected) == 4 and all(row['rc'] == '0' and
                int(row['params_size']) == int(row['params_limit']) == size and
                len(bytes.fromhex(row['params_before'])) == len(bytes.fromhex(row['params_after'])) == size and
                int.from_bytes(bytes.fromhex(row['after'])[28:32], 'little') == 0
                for row in selected), 'full private packet contract differs: ' + label + command)
    unsupported = [row for row in rows if row.get('rm_command') == '0x80028b']
    c.check(len(unsupported) == 4 and all(row['rc'] == '0' and
            int.from_bytes(bytes.fromhex(row['after'])[28:32], 'little') == 0x56 for row in unsupported),
            'native unsupported status lost: ' + label)
c.check(c.sha(c.read(folder, 'docker.strace')) == cuda['phases']['docker']['trace_sha256'], 'strace differs')
c.audit(ROOT, c.groups_in([prep] + reports))
runtime = c.load(ROOT, 'runtime-status.json')
c.check(runtime['status'] == 'finished' and runtime['exit_code'] == 0 and
        runtime['groups_empty'] and runtime['inputs_unchanged'], 'runtime audit differs')
print('Mapping v14 exact patch replay; features 31/31 and allocations 74/74.')
print('Four-card CUDA compute+transfer verified native/Docker/native; 25 groups empty, normal shutdown.')
print('Concurrent processes/threads, extended memory lifecycle, --gpus integration and performance remain incomplete.')
