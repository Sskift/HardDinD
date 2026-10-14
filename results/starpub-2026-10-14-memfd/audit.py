import datetime,hashlib,json,pathlib,subprocess,sys
w=pathlib.Path('/root/harddind-rebuild'); base=w/'results';sys.path.insert(0,str(w/'scripts'))
from guest_cleanup import group_members
reports=[base/name/'report.json' for name in ['memfd-gpu-fd-v1','memfd-gpu-docker-v1','memfd-gpu-control-v1']]
reports += [base/'memfd-smoke-v1.json',base/'memfd-docker-suite-v1.json']
reports += sorted(base.glob('memfd-factor-v1-[0-9]*.json'))
reports += sorted(base.glob('memfd-bracket-v1-[0-9]*.json'))
reports += sorted((base/'memfd-gpu-docker-v1').rglob('state.json'))
groups=set();workers=set();other_pids=set()
def collect(value):
 if isinstance(value,dict):
  for k,v in value.items():
   if k=='process_group':groups.add(int(v))
   elif k=='worker_pids':workers.update(map(int,v))
   elif k in ['guest_pid','network_pid','supervisor_pid']:other_pids.add(int(v))
   else:collect(v)
 elif isinstance(value,list):
  for v in value:collect(v)
for p in reports:collect(json.loads(p.read_text()))
rows={str(g):dict(zip(['live','zombies'],group_members(g))) for g in sorted(groups)}
present=[pid for pid in sorted(workers|other_pids) if (pathlib.Path('/proc')/str(pid)).exists()]
source=w/'linux-7.2.10';manifest=json.loads((source/'.harddind-patches.json').read_text())
source_hashes={name:hashlib.sha256((source/name).read_bytes()).hexdigest()==value for name,value in manifest['files'].items() if value is not None}
command=[str(pathlib.Path('/root/udock/bin/docker-cli')),'--host=tcp://127.0.0.1:23760','--tlsverify','--tlscacert=/root/udock/certs/ca.pem','--tlscert=/root/udock/certs/cert.pem','--tlskey=/root/udock/certs/key.pem','info','--format','server={{.ServerVersion}} running={{.ContainersRunning}} images={{.Images}}']
r=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=30)
out={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':rows,'recorded_gpu_worker_pids':sorted(workers),'other_recorded_pids':sorted(other_pids),'remaining_recorded_pids':present,'no_residual_processes':not present and all(not r['live'] and not r['zombies'] for r in rows.values()),'kernel_source_matches_manifest':source_hashes,'kernel_patch_hashes':manifest['patches'],'udock_readonly_health':{'exit_code':r.returncode,'output':r.stdout.strip()}}
fixture=json.loads((base/'memfd-docker-fixture-v1.json').read_text())
def sha(path):
 h=hashlib.sha256()
 with pathlib.Path(path).open('rb') as f:
  for chunk in iter(lambda:f.read(8*1024*1024),b''): h.update(chunk)
 return h.hexdigest()
out['reports']=[str(p.relative_to(base)) for p in reports]
out['immutable_inputs']={
 'kernel':sha(w/'memfd-kernel-v1')=='d872149b71ff0026201bebe5a8f482bb883a3c6c47c56d02d4411e444766bc5a',
 'docker_disk':sha(fixture['disk'])==fixture['disk_sha256'],
 'docker_base_disk':sha(fixture['base'])==fixture['base_sha256']}
path=base/'memfd-cleanup-audit-v1.json' 
with path.open('x') as f:f.write(json.dumps(out,indent=2)+'\n')
print(json.dumps({'no_residual_processes':out['no_residual_processes'],'group_count':len(groups),'immutable_inputs':out['immutable_inputs'],'source_files_verified':all(source_hashes.values()),'udock_readonly_health':out['udock_readonly_health']},indent=2))
