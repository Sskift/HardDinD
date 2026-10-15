#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('checks', ROOT.parent / 'gpu-mapping-v7/recheck.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
c.archive(ROOT)
owner = c.load(ROOT, 'owner-status.json')
build = c.load(ROOT.parent / 'gpu-completion-v1', 'build-status.json')
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['exact_match'] and owner['async_series_applied'] and owner['inputs_unchanged']
assert owner['actual_files'] == owner['expected_files'] == build['source_after']
assert len(owner['actual_files']) == 33
assert owner['kernel_sha256'] == build['kernel_sha256']
assert len(owner['groups']) == 13 and all(v == [[], []] for v in owner['groups'].values())
for row in owner['commands']:
    assert row['exit_code'] == 0 and row['cleanup']['clean_shutdown']
for name, digest in owner['source_inputs'].items():
    assert c.sha(c.read(ROOT, 'repo/' + name)) == digest
for name, module, field, count in [
    ('completion-features', 'gpu_feature_checks', 'feature_observations', 31),
    ('completion-alloc', 'gpu_alloc_checks', 'alloc_observations', 74),
]:
    report = c.control(ROOT, name, module, field, count)
    assert report['sha256']['/root/harddind-rebuild/gpu-completion-owner-v1/kernel'] == build['kernel_sha256']
    assert report['phases']['guest']['async_wait_verified']
    assert report['phases']['guest']['memory_backing']['verified']
first = ROOT.parent / 'gpu-completion-integration-v1'
c.archive(first)
failed = c.load(first, 'owner-status.json')
assert failed['status'] == 'finished' and failed['exit_code'] == 1 and failed['groups_empty']
assert failed['exact_match']
assert [v['exit_code'] for v in failed['commands']] == [0, 1]
assert b'refusing to overwrite local kernel edit' in c.read(first, 'async-series.stderr')
assert not (first / 'runtime').exists()
print('Fresh full mapping series reproduces 33 tested files; async-only series and stub relocations pass.')
print('Real native/disabled/guest/native regression: control31/31, alloc74/74; 13 groups empty.')
print('Earlier empty-tree preparation error retained; no Docker/full-GPU/performance verdict from these controls.')
