from pathlib import Path
import json,hashlib,fcntl,subprocess,os,signal,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_gpu_integration','commands':[],'full_acceptance':False,'functional_rerun':False}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout=180,env=None):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=repo,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and row['cleanup']['clean_shutdown'],label
save();rc=1
try:
 deadline=time.monotonic()+1800
 while True:
  prior=json.loads((w/'gpu-completion-integration-v2/owner-status.json').read_text())
  if prior['status']=='finished':break
  if time.monotonic()>deadline:raise TimeoutError('previous owner')
  time.sleep(2)
 assert prior['groups_empty']
 with(w/'kernel-build.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);os.sched_setaffinity(0,{0,1,2,3});r['stage']='compile_and_replay'
  r['before']={str(p.relative_to(repo)):sha(p)for p in repo.rglob('*')if p.is_file()and'__pycache__'not in p.parts};save()
  call('compile',['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread',str(repo/'src/cpu_share_probe.c'),'-o',str(b/'cpu-share-probe')])
  env={**os.environ,'HARDDIND_SHARE_PROBE':str(b/'cpu-share-probe')}
  call('tests',['python3','-m','unittest','discover','-s','tests','-p','test_cpu_share_checks.py','-v'],180,env)
  call('rehydrate',['python3',str(repo/'scripts/rehydrate_shares_v6.py'),str(w/'cpu-shares-owner-v6/run/results/shares-v6'),'--out',str(b/'rehydrated')],30)
  r['after']={n:sha(repo/n)for n in r['before']};r['inputs_unchanged']=r['before']==r['after'];assert r['inputs_unchanged'];rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 r['groups']={str(c['pid']):group_members(c['pid'])for c in r['commands']if'pid'in c};r['groups_empty']=all(v==([],[])for v in r['groups'].values());r.update(status='finished',exit_code=rc);save()
