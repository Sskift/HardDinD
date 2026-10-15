
from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'preflight','pid':os.getpid(),'full_acceptance':False,'performance_acceptance':False,'commands':[],'sources_before':{str(p.relative_to(repo)):sha(p) for p in repo.rglob('*') if p.is_file() and '__pycache__' not in p.parts}}
def save():
 p=b/'owner-status.json.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def command(label,cmd,timeout=120,env=None):
 with (b/(label+'.stdout')).open('xb') as out,(b/(label+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=repo,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True)
  row={'label':label,'command':cmd,'pid':p.pid};r['commands'].append(row);save()
  try:row['exit_code']=p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:
   row['timed_out']=True;os.killpg(p.pid,signal.SIGINT)
   try:row['exit_code']=p.wait(timeout=120)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(timeout=15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 if row.get('timed_out') or not row['cleanup']['clean_shutdown']:raise RuntimeError('unclean '+label)
 return row['exit_code']
def groups(value):
 out=set()
 if isinstance(value,dict):
  if 'process_group' in value:out.add(int(value['process_group']))
  for x in value.values():out.update(groups(x))
 elif isinstance(value,list):
  for x in value:out.update(groups(x))
 return out
save();rc=1
try:
 prior=json.loads((w/'gpu-os-event-docker-owner-v1/owner-status.json').read_text())
 assert prior['status']=='finished' and prior['groups_empty'] and prior['inputs_unchanged']
 assert prior['final_state']['phase']=='stopped' and not prior['final_state']['forced_shutdown']
 assert all(group_members(g)==([],[]) for g in groups(prior))
 os.sched_setaffinity(0,{0,1,2,3});r['owner_host_affinity']=sorted(os.sched_getaffinity(0))
 with (w/'kernel-build.lock').open('a') as lock:
  deadline=time.monotonic()+1800
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>deadline:raise RuntimeError('EX lock deadline')
    time.sleep(1)
  r['stage']='host_validation';save()
  assert command('build-fixture-probe',['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread',str(repo/'src/cpu_share_probe.c'),'-o',str(b/'fixture-probe')])==0
  assert command('checker-and-real-capture-tests',['python3','-m','unittest','discover','-s','tests','-p','test_cpu_*checks.py'],env={**os.environ,'HARDDIND_SHARE_PROBE':str(b/'fixture-probe')},timeout=180)==0
 r['stage']='actual_docker_shares';save()
 rc=command('runtime',['python3',str(repo/'scripts/run-cpu-shares.py'),str(b/'run'),'--kernel',str(w/'cpu-cpuset-kernel-v1'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name','shares-v6','--phase-ms','12000','--stable-window-ms','4500','--overhead-budget-ms','15000'],timeout=1800)
 result=json.loads((b/'run/results/shares-v6/report.json').read_text());r['runtime_verdict']=result['verdict']
except BaseException:r['error']=traceback.format_exc();rc=1
finally:
 try:
  statepath=b/'run/instances/shares-v6/state.json'
  state=json.loads(statepath.read_text()) if statepath.exists() else {}
  if state and state.get('phase')!='stopped':
   command('owner-final-stop',['python3',str(repo/'scripts/harddind.py'),str(b/'run'),'shares-v6','stop'],timeout=120)
   state=json.loads(statepath.read_text())
  r['final_state']=state
  checked=groups(r)|groups(locals().get('result',{}))
  journal=b/'run/results/shares-v6/command-launches.jsonl'
  if journal.exists():checked.update(json.loads(line)['pid'] for line in journal.read_text().splitlines())
  audit={str(g):group_members(g) for g in sorted(checked)}
  dirty={g:x for g,x in audit.items() if x!=([],[])}
  if dirty:
   r['emergency_cleanup']={g:cleanup_group(int(g)) for g in dirty};rc=1
  (b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
  r['groups_empty']=not dirty
  r['sources_after']={n:sha(repo/n) for n in r['sources_before']};r['inputs_unchanged']=r['sources_before']==r['sources_after']
  r['normal_shutdown']=state.get('phase')=='stopped' and state.get('guest_exit_code')==0 and not state.get('forced_shutdown',True)
  if not r['inputs_unchanged'] or not r['normal_shutdown']:rc=1
 except BaseException:r['audit_error']=traceback.format_exc();rc=1
r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
