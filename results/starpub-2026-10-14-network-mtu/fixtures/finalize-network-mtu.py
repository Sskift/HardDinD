import fcntl,hashlib,importlib.util,json,pathlib,subprocess,time
w=pathlib.Path('/root/harddind-rebuild');out=w/'results'
lock=(w/'kernel-build.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
spec=importlib.util.spec_from_file_location('cleanup',w/'scripts/guest_cleanup.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
d=json.loads((out/'net-mtu-v2.json').read_text());files={name:sha(pathlib.Path(name)) for name in d['sha256']}
assert files==d['sha256'],'input changed after comparison'
assert d['comparison_complete']
for n in ['scripts/guest_cleanup.py','scripts/run-runtime.py','scripts/analyze-network.py','scripts/make-runtime-root.sh','sources.lock.json']:
 files[str(w/n)]=sha(w/n)
func=json.loads((out/'network-mtu9000-v1.json').read_text());assert func['subset_passed']
for n,h in func['sources'].items():assert sha(w/n)==h,n
assert func['final_run']['config']['disk_sha256']==files[str(w/'runtime-mtu.ext4')]
groups=[]
def add(label,c):
 live,zombies=m.group_members(c['process_group']);groups.append({'instance':label,'pgid':c['process_group'],'historical_clean_shutdown':c['clean_shutdown'],'live':live,'zombies':zombies})
for name in ['net-mtu-v1','net-mtu-v2']:
 d=json.loads((out/(name+'.json')).read_text())
 for row in d['runs']:
  state=json.loads((pathlib.Path(row['instance'])/'state.json').read_text())
  for key in ['guest_cleanup','network_cleanup']:add(row['instance']+'/'+key,state[key])
 add(name+'/native',d['native_cleanup'])
for key in ['first_run','final_run']:
 for c in ['guest_cleanup','network_cleanup']:add('network-mtu9000-v1/'+key+'/'+c,func[key][c])
add('network-mtu9000-v1/native',func['native_cleanup'])
health=subprocess.run(['/root/udock/bin/docker-cli','--host=tcp://127.0.0.1:23760','--tlsverify',
 '--tlscacert=/root/udock/certs/ca.pem','--tlscert=/root/udock/certs/cert.pem','--tlskey=/root/udock/certs/key.pem',
 'info','--format','server={{.ServerVersion}} running={{.ContainersRunning}} images={{.Images}}'],capture_output=True,text=True,timeout=30)
r={'kind':'network_mtu_final_audit','checked_at_unix':time.time(),'input_hashes_verified':True,'files':files,
   'groups':groups,'all_groups_empty':all(not g['live'] and not g['zombies'] for g in groups),
   'historical_forced_cleanup_retained':True,'old_service_health_exit_code':health.returncode,'old_service_health':health.stdout.strip(),
   'persistent_cows_retained':True,'full_acceptance':False}
with (out/'net-mtu-audit.json').open('x') as f:json.dump(r,f,indent=2);f.write('\n')
print(json.dumps({'all_empty':r['all_groups_empty'],'groups':len(groups),'old_service_health':r['old_service_health']}))
