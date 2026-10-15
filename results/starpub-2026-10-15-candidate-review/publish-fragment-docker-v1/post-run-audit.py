from pathlib import Path
import datetime,hashlib,json,sys
b=Path(__file__).resolve().parent;repo=b/'repo';sys.path.insert(0,str(repo/'scripts'));import harddind
from guest_cleanup import group_members
owner_path=b/'owner-status.json';report_path=b/'run/results/pubfrag1.json';owner=json.loads(owner_path.read_text());report=json.loads(report_path.read_text())
assert owner['status']=='finished'and owner['exit_code']==1 and "KeyError: 'pgid'"in owner['cleanup_error']
assert owner['commands'][0]['exit_code']==0 and owner['commands'][0]['cleanup']['clean_shutdown']
assert report['subset_passed'] and all(x=='pass'for x in report['checks'].values())and len(report['checks'])==21
assert len(report['commands'])==121 and len(report['echoes'])==140 and all(x['verified']and x['sent_bytes']==x['received_bytes']for x in report['echoes'])
groups={x['cleanup']['process_group']for x in report['commands']};groups.update(x['pid']for x in owner['commands'])
for state in [report['first_run'],report['final_run']]:
 assert state['phase']=='stopped'and state['guest_exit_code']==0 and not state['forced_shutdown']
 assert state['guest_cleanup']['clean_shutdown']and state['network_cleanup']['clean_shutdown']
 groups.update(state[k]for k in ['guest_pid','network_pid','supervisor_pid']if state.get(k))
audit={'owner_sha256':harddind.sha(owner_path),'runtime_report_sha256':harddind.sha(report_path),'original_wrapper_exit_code':1,'wrapper_error':"KeyError: 'pgid'; correct cleanup field is process_group",'functional_subset_passed':report['subset_passed'],'full_acceptance':False,'performance_acceptance':False,'groups':{str(g):group_members(g)for g in sorted(groups)}}
audit['groups_empty']=all(v==([],[])for v in audit['groups'].values())
audit['source_after']={str(p.relative_to(repo)):harddind.sha(p)for p in repo.rglob('*')if p.is_file()and '__pycache__'not in p.parts};audit['source_unchanged']=owner['source_before']==audit['source_after']
audit['sha256_after']={name:harddind.sha(Path(name))for name in report['sha256']};audit['inputs_unchanged']=report['inputs_unchanged']and report['sha256']==audit['sha256_after']
audit['all_commands_expected']=all(x['expected_exit_satisfied']and not x['timed_out']and x['cleanup']['clean_shutdown']for x in report['commands'])
audit['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();audit['audit_passed']=all(audit[k]for k in ['groups_empty','source_unchanged','inputs_unchanged','all_commands_expected'])
(b/'post-run-audit.json').write_text(json.dumps(audit,indent=2)+'\n');print(json.dumps({k:audit[k]for k in ['functional_subset_passed','groups_empty','source_unchanged','inputs_unchanged','all_commands_expected','audit_passed']}));print('groups',len(groups));assert audit['audit_passed']
