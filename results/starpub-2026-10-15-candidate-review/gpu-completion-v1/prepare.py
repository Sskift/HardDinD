from pathlib import Path
import datetime,fcntl,hashlib,json,os,shutil,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;prev=w/'gpu-mapping-owner-v14';src=w/'arkclaude/round5/gpu-mapping-repair/.work/linux-7.2.10';build=src.parent/'build'
sys.path.insert(0,str(prev/'repo/scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_v3','pid':os.getpid(),'full_acceptance':False,'commands':[]}
def save():
 p=b/'build-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'build-status.json')
def call(label,cmd,timeout=180,cwd=None):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=cwd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and row['cleanup']['clean_shutdown'],label
 return row
save();rc=1
try:
 until=time.monotonic()+900
 while True:
  prior=json.loads((w/'gpu-instrumentation-owner-v3/owner-status.json').read_text())
  if prior['status']=='finished':break
  if time.monotonic()>until:raise TimeoutError('prior GPU matrix')
  time.sleep(2)
 assert prior['groups_empty'] and prior['normal_shutdown']
 with(w/'kernel-build.lock').open('a')as lock:
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>until:raise TimeoutError('build lock')
    time.sleep(1)
  os.sched_setaffinity(0,{0,1,2,3});r['stage']='patch_and_build';save()
  before=json.loads((prev/'preparation-status.json').read_text())['source_sha256_after'];r['source_before']={n:sha(src/n)for n in before};assert r['source_before']==before
  r['config_before']=sha(build/'.config');save()
  call('patch',['patch','--batch','--forward','--fuzz=0','-p1','-i',str(b/'0028-um-gpu-completion-ownership.patch')],30,src)
  r['source_build']={n:sha(src/n)for n in before};changed=[n for n in before if before[n]!=r['source_build'][n]];assert changed==['arch/um/kernel/skas/stub.c'],changed
  for n in before:
   p=b/'source'/n;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src/n,p)
  call('build',['make','-C',str(src),'O='+str(build),'ARCH=um','-j4'],360)
  r['source_after']={n:sha(src/n)for n in before};assert r['source_build']==r['source_after'];assert r['config_before']==sha(build/'.config')
  shutil.copy2(build/'linux',b/'kernel');shutil.copy2(build/'.config',b/'kernel.config');r['kernel_sha256']=sha(b/'kernel');r['patch_sha256']=sha(b/'0028-um-gpu-completion-ownership.patch')
  # Verify exactly one patch on the frozen v14 file.
  target=b/'reapplied/arch/um/kernel/skas/stub.c';target.parent.mkdir(parents=True);shutil.copy2(prev/'source/arch/um/kernel/skas/stub.c',target)
  call('reapply',['patch','--batch','--forward','--fuzz=0','-p1','-i',str(b/'0028-um-gpu-completion-ownership.patch')],30,b/'reapplied');assert target.read_bytes()==(src/'arch/um/kernel/skas/stub.c').read_bytes()
  shutil.copytree(w/'gpu-instrumentation-owner-v2/repo',b/'repo',ignore=shutil.ignore_patterns('__pycache__'));shutil.copy2(b/'0028-um-gpu-completion-ownership.patch',b/'repo/kernel/experiments/0028-um-gpu-completion-ownership.patch')
  run=b/'run';run.mkdir()
  for n in ['client','network-backend','kernel-build.lock']:(run/n).symlink_to(w/n)
  runtime=(w/'gpu-instrumentation-owner-v2/supervisor.py').read_text().replace("name='gpu-instrument-v2'","name='gpu-complete-v1'").replace("kernel=old/'kernel'","kernel=b/'kernel'")
  (b/'supervisor.py').write_text(runtime);rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 r['groups']={str(c['pid']):group_members(c['pid'])for c in r['commands']if'pid'in c};r['groups_empty']=all(v==([],[])for v in r['groups'].values());r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
if rc==0:
 with(b/'supervisor.log').open('x')as f:q=subprocess.Popen(['python3',str(b/'supervisor.py')],stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
 (b/'runtime-launch.json').write_text(json.dumps({'pid':q.pid})+'\n')
