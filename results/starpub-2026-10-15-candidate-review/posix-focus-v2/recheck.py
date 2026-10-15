#!/usr/bin/env python3
"""Check frozen focus runs, including the earlier probe pipe failure."""
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for version in (1, 2):
    root = ROOT.parent / f'posix-focus-v{version}'
    def read(name):
        path = root / name
        return path.read_bytes() if path.exists() else gzip.decompress(
            path.with_name(path.name + '.gz').read_bytes())
    for name, digest in json.loads(read('archive-sha256.json')).items():
        assert hashlib.sha256(read(name)).hexdigest() == digest, name
    owner = json.loads(read('owner-status.json'))
    assert owner['status'] == 'finished' and owner['groups_empty'] and owner['inputs_unchanged']
    assert owner['exit_code'] == (1 if version == 1 else 0)
    assert owner['backend_sha256'] == '13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09'
    for name, digest in owner['sources'].items():
        assert hashlib.sha256(read('repo/' + name)).hexdigest() == digest
    state = owner['final_state']
    assert state['phase'] == 'stopped' and not state['forced_shutdown']
    assert state['guest_exit_code'] == state['network_exit_code'] == 0
    assert state['guest_cleanup']['clean_shutdown'] and state['network_cleanup']['clean_shutdown']
    assert all(x['clean_shutdown'] and x['exit_code'] == 0 for x in state['filesystem_cleanup'])
    audit = json.loads(read('owner-cleanup-audit.json'))
    assert len(audit) == 32 and all(x == [[], []] for x in audit.values())
    assert len(owner['commands']) == 7
    for row in owner['commands']:
        assert row['exit_code'] == (1 if version == 1 and row['label'] == 'nested-enabled' else 0)
        assert row['cleanup']['clean_shutdown'] and not row.get('timed_out')
    for key, filename, expected in [
        ('native', 'native', (10, 2)), ('default', 'docker-default', (11, 1)),
        ('nested-enabled', 'docker-nested-enabled', (11 if version == 1 else 12, 0))]:
        report = json.loads(read('runtime/' + filename + '.json'))
        assert report == owner['reports'][key]
        assert (report['tested'], report['not_tested']) == expected and report['complete']
        assert report['probe_sha256'] == owner['probe_sha256']
        assert report['failures'] == (['getlk_nested_pidns_local_vs_ancestor']
                                      if version == 1 and key == 'nested-enabled' else [])
        for peer in report['peers']:
            assert peer['returncode'] == 0 and not peer['forced'] and peer['cleanup']['clean_shutdown']
            assert str(peer['pid']) in audit
        if key != 'native':
            assert report['checks']['getlk_sibling_container_pid_zero']['status'] == 'tested'
    nested = owner['reports']['nested-enabled']['checks']['getlk_nested_pidns_local_vs_ancestor']['detail']
    assert nested['holder_ancestor_pid'] == 8 and nested['holder_local_pid'] == 1
    assert (nested['getlk_errno'], nested['getlk_local_pid'], nested['querier_local_pid']) == (
        (5, -1, -1) if version == 1 else (0, 1, 2))
print('Focus v1 probe-pipe failure retained; v2 native 10/2, Docker 11/1 and 12/0 tested/not-tested.')
print('Both runs stopped normally; 32 recorded groups per run empty; no performance claim.')
