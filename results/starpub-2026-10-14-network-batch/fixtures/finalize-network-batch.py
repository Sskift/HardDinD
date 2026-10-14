import fcntl, hashlib, importlib.util, json, pathlib, subprocess, time
w=pathlib.Path('/root/harddind-rebuild');out=w/'results'
lock=(w/'kernel-build.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
spec=importlib.util.spec_from_file_location('cleanup',w/'scripts/guest_cleanup.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
d=json.loads((out/'net-batch-v2.json').read_text())
files={name:sha(pathlib.Path(name)) for name in d['sha256']}
assert files==d['sha256'],'input changed after measurement'
assert d['comparison_complete']
files[str(w/'network-backend-before-batch')]=sha(w/'network-backend-before-batch')
files[str(w/'network-backend-batch-v1')]=sha(w/'network-backend-batch-v1')
groups=[]
for version in (1,2):
 d=json.loads((out/f'net-batch-v{version}.json').read_text())
 assert d['comparison_complete']
 for run in d['runs']:
  assert run['stop']['phase']=='stopped'
  for key in ['guest_cleanup','network_cleanup']:
   c=run['stop'][key];live,zombies=m.group_members(c['process_group'])
   groups.append({'instance':run['instance'],'kind':key,'pgid':c['process_group'],
                  'historical_clean_shutdown':c['clean_shutdown'],'live':live,'zombies':zombies})
 c=d['native_cleanup'];live,zombies=m.group_members(c['process_group'])
 groups.append({'instance':f'native-v{version}','pgid':c['process_group'],'live':live,'zombies':zombies})
health=subprocess.run(['/root/udock/bin/docker-cli','--host=tcp://127.0.0.1:23760','--tlsverify',
 '--tlscacert=/root/udock/certs/ca.pem','--tlscert=/root/udock/certs/cert.pem','--tlskey=/root/udock/certs/key.pem',
 'info','--format','server={{.ServerVersion}} running={{.ContainersRunning}} images={{.Images}}'],capture_output=True,text=True,timeout=30)
r={'kind':'network_batch_final_audit','checked_at_unix':time.time(),'input_hashes_verified':True,'files':files,
   'groups':groups,'all_groups_empty':all(not g['live'] and not g['zombies'] for g in groups),
   'old_service_health_exit_code':health.returncode,'old_service_health':health.stdout.strip(),
   'persistent_cows_retained':True,'full_acceptance':False}
with (out/'net-batch-audit.json').open('x') as f:json.dump(r,f,indent=2);f.write('\n')
print(json.dumps({'all_empty':r['all_groups_empty'],'groups':len(groups),'old_service_health':r['old_service_health']}))
