from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';run=b/'run'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'preflight','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False,'performance_acceptance':False,'sources_before':{str(p.relative_to(repo)):sha(p) for p in repo.rglob('*') if p.is_file() and '__pycache__' not in p.parts},'binary_sha256':sha(run/'virtiofsd'),'libs_sha256':{str(p):sha(p) for p in (b/'libs').iterdir()}}
def save():(b/'owner-status.json').write_text(json.dumps(r,indent=2)+'\n')
def groups(value):
 out=set()
 if isinstance(value,dict):
  if 'process_group' in value and 'clean_shutdown' in value:out.add(value['process_group'])
  for child in value.values():out.update(groups(child))
 elif isinstance(value,list):
  for child in value:out.update(groups(child))
 return out
save();rc=1
try:
 prior=w/'gpu-lifecycle-owner-v5'
 previous=json.loads((prior/'owner-report.json').read_text());assert previous['status']=='finished' and previous['groups_empty']
 before_groups=groups(previous)
 audit={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':{str(g):group_members(g) for g in sorted(before_groups)}};audit['all_empty']=all(not a and not z for a,z in audit['groups'].values());assert audit['all_empty']
 (b/'preflight-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
 os.sched_setaffinity(0,{0,1,2,3});r['stage']='build_probe';save()
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  cmd=['gcc','-O2','-Wall','-Wextra','-Werror','-static',str(repo/'src/share_probe.c'),'-o',str(run/'share-probe')]
  with (b/'build-probe.log').open('x') as out:
   p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
   try:code=p.wait(timeout=60)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=15);raise
   finally:r['build_cleanup']=cleanup_group(p.pid);save()
  assert code==0 and r['build_cleanup']['clean_shutdown'];r['probe_sha256']=sha(run/'share-probe')
 # Release build EX before acquiring runtime SH; the guest itself also holds SH.
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_SH);r['stage']='actual_shared_locks';save()
  cmd=['python3',str(repo/'scripts/run-shared-locks.py'),str(run),'--kernel',str(w/'cpu-cpuset-kernel-v1'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name','posix-v6','--virtiofsd',str(run/'virtiofsd'),'--posix-locks','--inline-flock','--functional-only']
  with (b/'runtime.log').open('x') as out:
   p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,env={**os.environ,'LD_LIBRARY_PATH':str(b/'libs')},start_new_session=True);r['runtime_pid']=p.pid;r['runtime_command']=cmd;save()
   try:rc=p.wait(timeout=900)
   except subprocess.TimeoutExpired:
    stop=subprocess.run(['python3',str(repo/'scripts/harddind.py'),str(run),'posix-v6','stop'],capture_output=True,text=True,timeout=120);r['timeout_stop']={'rc':stop.returncode,'stdout':stop.stdout,'stderr':stop.stderr};os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=15);raise
   finally:r['runtime_cleanup']=cleanup_group(p.pid);save()
  result=json.loads((run/'results/posix-v6.json').read_text());r['functional_complete']=result['functional_complete'];r['all_checks_passed']=result['all_checks_passed']
 r['inputs_unchanged']=all(sha(repo/n)==h for n,h in r['sources_before'].items()) and sha(run/'virtiofsd')==r['binary_sha256'] and all(sha(Path(n))==h for n,h in r['libs_sha256'].items());assert r['inputs_unchanged']
 audit_groups=groups(result)|groups(r);audit={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':{str(g):group_members(g) for g in sorted(audit_groups)}};audit['all_empty']=all(not a and not z for a,z in audit['groups'].values());(b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n');assert audit['all_empty']
except BaseException:r['error']=traceback.format_exc();rc=1
r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
