#!/usr/bin/env python3
"""Replay identical OS-event ABI captures across native/Docker/native."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def read(name):
    path = ROOT / name
    return path.read_bytes() if path.exists() else gzip.decompress(
        path.with_name(path.name + '.gz').read_bytes())
for name, digest in json.loads(read('archive-sha256.json')).items():
    assert hashlib.sha256(read(name)).hexdigest() == digest, name
owner = json.loads(read('owner-status.json'))
assert owner['status'] == 'finished' and owner['exit_code'] == 0
assert owner['groups_empty'] and owner['inputs_unchanged']
for name, digest in owner['sources'].items():
    assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest, name
native = json.loads((ROOT.parent / 'gpu-os-event-v2/owner-report.json').read_text())
assert owner['binary_sha256'] == native['binary_sha256']
assert owner['kernel_sha256'] == '23efcdd84f69104170b1c085eca57a257dae3b88a1fe194cb074dd3205c9c729'
assert len(owner['commands']) == 19
for row in owner['commands']:
    assert row['exit_code'] == 0 and not row.get('timed_out') and row['cleanup']['clean_shutdown']
state = owner['final_state']
assert state['phase'] == 'stopped' and not state['forced_shutdown']
assert state['guest_exit_code'] == state['network_exit_code'] == 0
assert state['guest_cleanup']['clean_shutdown'] and state['network_cleanup']['clean_shutdown']
audit = json.loads(read('owner-cleanup-audit.json'))
assert len(audit) == 19 and all(x == [[], []] for x in audit.values())
post = json.loads(read('owner-guest-postcheck.json'))
assert post['all_empty'] and len(post['groups']) == 3
assert all(x == [[], []] for x in post['groups'].values())
spec = importlib.util.spec_from_file_location('events', ROOT / 'repo/scripts/gpu_os_event_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
for phase in ('native-before', 'docker', 'native-after'):
    for index in range(4):
        label = f'{phase}-{index}'
        raw = read(label + '.stdout').decode()
        verdict = checks.analyze(raw, {'device': f'/dev/nvidia{index}', 'ctl_device': '/dev/nvidiactl', 'poll_ms': 30})
        assert verdict['ok'] and dict(verdict, process_ok=True) == owner['verdicts'][label]
        summary = json.loads(raw.splitlines()[-1])
        assert summary['clients_created'] == summary['clients_freed'] == 9
        assert summary['allocations_ok'] == summary['frees_ok'] == 7
        assert summary['fds_opened'] == summary['fds_closed'] == 3
print('Four native + four Docker + four native OS-event ABI captures pass the frozen checker.')
print('19 command groups and 3 runtime groups empty; normal stop. CUDA/performance remain unverified.')
