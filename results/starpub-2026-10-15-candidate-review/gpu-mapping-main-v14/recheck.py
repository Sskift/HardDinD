#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
for name, digest in json.loads(read('source-manifest.json')).items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest
result = json.loads(read('owner-status.json'))
assert result['status'] == 'finished' and result['exit_code'] == 0
assert result['exact_match'] and result['runtime_config_matches'] and result['groups_empty']
assert result['cleanup']['clean_shutdown'] and not result['cleanup']['forced_cleanup']
candidate = json.loads((ROOT.parent / 'gpu-mapping-v14/preparation-status.json').read_text())
assert result['actual_files'] == result['expected_files'] == candidate['source_sha256_after']
assert len(result['actual_files']) == 33
assert hashlib.sha256(read('repo/kernel/experiments/0024-um-gpu-mapping-lifecycle.patch')).hexdigest() == candidate['patch_sha256']
assert '--gpu-mapping-experiment' in result['command']
print('Main optional mapping selector reproduces all 33 files of the four-card CUDA-tested kernel.')
