#!/usr/bin/env python3
"""Replay raw command receipts and versioned functional verdicts, not performance acceptance."""
from pathlib import Path
import ast,hashlib,json,math,statistics,tarfile
root=Path(__file__).resolve().parent
export=json.loads((root/'export.json').read_text())
sha=lambda data:hashlib.sha256(data).hexdigest()
results={};groups=set();command_count=0
for version,entry in export.items():
    archive=root/('v'+version+'-evidence.tar.gz')
    assert sha(archive.read_bytes())==entry['archive_sha256']
    with tarfile.open(archive) as tar:
        files={m.name:tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    manifest=json.loads(files['manifest.json'])
    assert set(files)==set(manifest)|{'manifest.json'}
    assert all(sha(files[name])==digest for name,digest in manifest.items())
    audit=json.loads((root/('v'+version+'-audit.json')).read_text())
    assert audit['groups_empty'] and audit['input_hashes_unchanged']
    assert all(p==[[],[]] for p in audit['groups'].values())
    groups.update(audit['groups'])
    syntax=ast.parse(files['repo/scripts/run-workflow-timing.py'])
    definitions={}
    for node in syntax.body:
        if isinstance(node,ast.Assign):
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in ('REQUIRED_CHECKS','REQUIRED_STAGES'):
                    definitions[target.id]=ast.literal_eval(node.value)
    for name in entry['reports']:
        r=json.loads(files['run/results/'+name+'.json'])
        assert r['inputs_unchanged'] and r['input_hashes_before']==r['input_hashes_after']
        assert r['native_docker_comparison']=='not_tested' and not r['full_acceptance'] and not r['near_native_acceptance']
        assert not r['performance_threshold_enforced']
        for path,digest in r['input_hashes_before'].items():
            if '/repo/' in path:assert sha(files['repo/'+path.split('/repo/',1)[1]])==digest
        assert r['instance_cleanup']['clean_shutdown'] and not r['instance_cleanup']['forced_shutdown']
        assert r['instance_cleanup']['guest_exit_code']==0
        components=list(r['fixture_components'])+list(r['leftover_components'])
        statuses=[]
        for workflow,data in r['workflows'].items():
            observations=data['observations']
            assert len(observations)==r['reps_requested']==3
            assert [o['rep'] for o in observations]==[0,1,2]
            body=[]
            for obs in observations:
                statuses.append(obs['status'])
                components.extend(obs['components'])
                if obs['status']=='not_run_wall_budget':continue
                assert math.isfinite(obs['body_seconds']) and obs['body_seconds']>0
                body.append(obs['body_seconds'])
                checks={c['name']:c['ok'] for c in obs['checks']}
                stages={c['stage'] for c in obs['components'] if not c.get('synthetic')}
                if obs['status']=='pass':
                    assert all(checks.get(k) is True for k in definitions['REQUIRED_CHECKS'][workflow])
                    assert all(s in stages for s in definitions['REQUIRED_STAGES'][workflow])
                    assert not obs['error'] and not obs['cleanup_errors'] and obs['reaped_cleanly']
                    assert all(not c.get('timed_out') for c in obs['components'])
                else:
                    assert (obs.get('error') or obs['cleanup_errors'] or not all(checks.values())
                            or set(definitions['REQUIRED_CHECKS'][workflow])-set(checks)
                            or set(definitions['REQUIRED_STAGES'][workflow])-stages)
                if workflow=='build' and 'warm_content_equal' in checks and obs['status']=='pass':
                    content=obs['build_image_content']
                    assert content['cold']==content['warm'] and content['cold']['RootFS']['Layers']
                    for c in obs['components']:
                        if c['stage'] in ['run_cold','run_warm']:
                            assert c['docker_args'][-2:]==['sha256sum','/wf/payload.bin']
                            assert c['stdout'].split()[0]==r['inputs']['payload_64kib']['sha256']
            summary=r['summary'][workflow]
            assert summary['passed']==sum(o['status']=='pass' for o in observations)
            assert summary['repeated_observations']==(summary['passed']>=3)
            if body:assert summary['observation_body_seconds']['median_seconds']==statistics.median(body)
        prefix='/root/harddind-rebuild/workflow-owner-v'+version+'/'
        for c in components:
            if c.get('synthetic'):continue
            assert math.isfinite(c['seconds']) and c['seconds']>=0
            assert c['cleanup']['clean_shutdown']
            assert str(c['pid']) in audit['groups']
            if c['status']=='ok':assert not c['timed_out'] and c['exit_code'] in c['expect']
            for channel in ['stdout','stderr']:
                raw=c['raw'][channel];path=raw['path'];assert path.startswith(prefix)
                payload=files[path[len(prefix):]]
                assert len(payload)==raw['bytes'] and sha(payload)==raw['sha256']
                if not c['truncated']:assert payload.decode(errors='replace')==c[channel]
            command_count+=1
        for c in [r['setup']['boot'],r['instance_cleanup']['stop_command']]:
            assert c['exit_code']==0 and not c['timed_out'] and c['cleanup']['clean_shutdown']
            assert str(c['pid']) in audit['groups'];command_count+=1
        expected=all(s=='pass' for s in statuses) and not r['leftovers']['objects'] and not r['leftovers']['scan_failed']
        assert r['subset_passed']==expected
        results[name]={'subset_passed':r['subset_passed'],'summary':r['summary'], 'problems':r['problems'],
                       'total_wall_seconds':r['total_wall_seconds']}
        assert results[name]==audit['reports'][name]
print(json.dumps({'verified_reports':len(results),'command_receipts':command_count,'empty_process_groups':len(groups),
                  'passed_runs':[n for n,r in results.items() if r['subset_passed']],
                  'preserved_failed_runs':[n for n,r in results.items() if not r['subset_passed']],
                  'native_docker_comparison':'not_tested','full_acceptance':False},indent=2))
