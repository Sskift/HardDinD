#!/usr/bin/env python3
"""Reapply the frozen v11 patch and reparse its actual control/CUDA evidence."""
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
with tempfile.TemporaryDirectory(prefix='harddind-mapping-v11-') as tmp:
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
        '3f3f97080af4fbb92662d346e9cd39016a1722ae39e83ed35d827f1fe12f7380',
        'kernel binding differs')
reports = [c.control(ROOT, 'mapping-features-v11', 'gpu_feature_checks', 'feature_observations', 31),
           c.control(ROOT, 'mapping-alloc-v11', 'gpu_alloc_checks', 'alloc_observations', 74),
           c.cuda(ROOT, 11, kernel)]
c.audit(ROOT, c.groups_in([prep] + reports))
runtime = c.load(ROOT, 'runtime-status.json')
c.check(runtime['status'] == 'finished' and runtime['exit_code'] == 1 and
        runtime['groups_empty'] and runtime['inputs_unchanged'], 'runtime boundary changed')
cuda = reports[-1]
c.check(cuda['diagnostic_complete'] and cuda['device_source_prefix'] == '/dev/nvidia' and
        cuda['uvm_device_source'] == '/dev/nvidia-uvm', 'standard device selection differs')
rows = []
for line in c.read(ROOT, 'runtime/mapping-cuda-v11/docker.stderr').decode().splitlines():
    if line.startswith('CUDA_IOCTL '):
        rows.append(dict(token.split('=', 1) for token in line.split() if '=' in token))
errors = [(r['request'], r.get('rm_command'), r.get('params_size'), r['errno'])
          for r in rows if r['rc'] == '-1']
c.check(len(errors) == 8 and
        errors.count(('0xc020462a', '0x20810108', '992', '95')) == 4 and
        errors.count(('0xc020462a', '0x80028b', '1', '95')) == 4,
        'actual remaining failures differ')
for request in ('0xc01046ce', '0xc01046cf'):
    events = [r for r in rows if r['request'] == request]
    c.check(len(events) == 4 and all(r['rc'] == '0' and
            int.from_bytes(bytes.fromhex(r['after'])[12:16], 'little') == 0
            for r in events), 'event registration/release failed: ' + request)
numa = [r for r in rows if r['request'] == '0xc23046d7']
detach = [r for r in rows if r.get('rm_command') == '0x216']
c.check(len(numa) == 4 and all(r['rc'] == '0' for r in numa), 'NUMA query did not advance')
c.check(not detach, 'this run did not execute the newly allowed detach control')
c.check(b'cuInit(0): CUDA error 100' in c.read(ROOT, 'runtime/mapping-cuda-v11/docker.stderr'),
        'internal CUDA failure missing')
print(json.dumps({'patch_reapply': 'exact', 'features': '31/31', 'allocation': '74/74',
                  'numa_queries': '4 successful', 'os_events': '4 allocations and 4 releases successful', 'internal_cuda': 'fail',
                  'detach_control': 'not exercised in this run',
                  'remaining_errors': {'control_0x20810108': 4, 'control_0x80028b': 4,
                                       'alloc_os_event': 0},
                  'native_before_after': 'four GPU compute/transfer results verified',
                  'cleanup': 'all recorded groups independently empty'}, indent=2))
