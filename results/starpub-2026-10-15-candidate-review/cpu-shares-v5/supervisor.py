from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_lock','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False,'performance_acceptance':False,'commands':[],'sources_before':{str(p.relative_to(repo)):sha(p) for p in repo.rglob('*') if p.is_file() and '__pycache__' not in p.parts}}
def save():(b/'owner-status.json').write_text(json.dumps(r,indent=2)+'\n')
def command(label,cmd,timeout=60,env=None):
 with (b/(label+'.stdout')).open('xb') as out,(b/(label+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=repo,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True)
  timed=False
  try:code=p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:timed=True;os.killpg(p.pid,signal.SIGKILL);code=p.wait(timeout=15)
  finally:cleanup=cleanup_group(p.pid)
 r['commands'].append({'label':label,'command':cmd,'pid':p.pid,'exit_code':code,'timed_out':timed,'cleanup':cleanup});save()
 if timed or not cleanup['clean_shutdown']:raise RuntimeError('unclean '+label)
 return code
save();rc=1
try:
 deadline=time.monotonic()+1800
 prior=w/'cpu-witness-owner-v3'
 while json.loads((prior/'owner-status.json').read_text())['status']!='finished':
  if time.monotonic()>deadline:raise RuntimeError('prior witness run did not finish')
  time.sleep(1)
 prior_status=json.loads((prior/'owner-status.json').read_text());assert prior_status['groups_empty'] and prior_status['inputs_unchanged']
 prior_report=json.loads((prior/'run/results/witness-v3/report.json').read_text());assert prior_report['normal_shutdown']
 audit=json.loads((prior/'owner-cleanup-audit.json').read_text());current={g:group_members(int(g)) for g in audit};assert all(x==([],[]) for x in current.values())
 r['prior_groups_empty']=current;save()
 os.sched_setaffinity(0,{0,1,2,3});r['owner_host_affinity']=sorted(os.sched_getaffinity(0))
 with (w/'kernel-build.lock').open('a') as lock:
  deadline=time.monotonic()+1800
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>deadline:raise RuntimeError('EX lock deadline')
    time.sleep(1)
  r['stage']='owner_host_validation';save()
  assert command('build-fixture-probe',['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread',str(repo/'src/cpu_share_probe.c'),'-o',str(b/'fixture-probe')])==0
  assert command('checker-and-real-capture-tests',['python3','-m','unittest','discover','-s','tests','-p','test_cpu_*checks.py'],env={**os.environ,'HARDDIND_SHARE_PROBE':str(b/'fixture-probe')})==0
 r['stage']='actual_docker_shares';save()
 cmd=['python3',str(repo/'scripts/run-cpu-shares.py'),str(b/'run'),'--kernel',str(w/'cpu-cpuset-kernel-v1'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name','shares-v5','--phase-ms','12000','--stable-window-ms','4500','--overhead-budget-ms','15000']
 # Runner owns its bounded command deadlines and final stop. Do not kill it
 # on a shorter aggregate timeout before its cleanup can complete.
 with (b/'runtime.log').open('x') as out:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT);r['runtime_pid']=p.pid;r['runtime_command']=cmd;save();rc=p.wait(timeout=1800)
 r['runtime_verdict']=json.loads((b/'run/results/shares-v5/report.json').read_text())['verdict']
 def groups(value):
  out=set()
  if isinstance(value,dict):
   if 'process_group' in value and 'clean_shutdown' in value:out.add(value['process_group'])
   for child in value.values():out.update(groups(child))
  elif isinstance(value,list):
   for child in value:out.update(groups(child))
  return out
 result=json.loads((b/'run/results/shares-v5/report.json').read_text())
 checked=groups(result)|groups(r)
 audit={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':{str(g):group_members(g) for g in sorted(checked)}}
 audit['all_empty']=all(not a and not z for a,z in audit['groups'].values())
 (b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n');assert audit['all_empty']
 r['sources_after']={n:sha(repo/n) for n in r['sources_before']};r['inputs_unchanged']=r['sources_before']==r['sources_after'];assert r['inputs_unchanged']
except BaseException:r['error']=traceback.format_exc();rc=1
r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
