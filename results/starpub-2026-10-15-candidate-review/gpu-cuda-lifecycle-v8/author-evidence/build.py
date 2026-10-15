from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback,shutil
b=Path(__file__).resolve().parent;w=b.parent
sys.path.insert(0,str(w/'gpu-completion-owner-v1/repo/scripts'))
from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
prepared=json.loads((b/'prepare.json').read_text())
r={'status':'running','stage':'waiting_cpu_v2','commands':[], 'source_before':prepared['source_before'],
   'config_before':prepared['config_sha256'],'patch_sha256':prepared['patch_sha256'],
   'full_acceptance':False,'runtime_acceptance':False,'affinity':[0,1,2,3]}
def save():
 p=b/'build-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'build-status.json')
def acquire():
 f=(w/'kernel-build.lock').open('a');end=time.monotonic()+300
 while True:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);return f
  except BlockingIOError:
   if time.monotonic()>=end:f.close();raise TimeoutError('global lock wait300s')
   time.sleep(.25)
def call(label,cmd,timeout):
 row={'label':label,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,stdout=out,stderr=err,stdin=subprocess.DEVNULL,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:
   row['timeout']=True;os.killpg(p.pid,signal.SIGTERM)
   try:row['exit_code']=p.wait(10)
   except subprocess.TimeoutExpired:row['sigkill']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(10)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert not row['cleanup']['remaining_live_pids']and not row['cleanup']['remaining_zombie_pids'],label+' residuals'
 return row
save();rc=1
try:
 os.sched_setaffinity(0,set(r['affinity']));end=time.monotonic()+1800
 while True:
  prior=w/'cpu-faultaround-owner-v2/owner-status.json'
  if prior.exists():
   q=json.loads(prior.read_text())
   if q['status']=='finished':assert q['groups_empty'];break
  if time.monotonic()>=end:raise TimeoutError('CPU owner queue1800s')
  time.sleep(1)
 assert all(sha(b/'linux'/n)==h for n,h in r['source_before'].items())
 assert sha(b/'build/.config')==r['config_before']
 assert sha(b/'0029-um-gpu-usercopy-lock-order.patch')==r['patch_sha256']
 r['stage']='configure';save()
 common=['make','-C',str(b/'linux'),'O='+str(b/'build'),'ARCH=um']
 with acquire():
  c=call('olddefconfig',common+['olddefconfig'],60);assert c['exit_code']==0 and not c.get('timeout')
  assert (b/'build/.config').read_bytes()==(b/'kernel.config').read_bytes(),'config drift'
 r['stage']='build';save()
 for i in range(12):
  with acquire():c=call('make-'+str(i),common+['-j4','linux'],150)
  if c['exit_code']==0 and not c.get('timeout'):break
  if not c.get('timeout'):raise RuntimeError('non-timeout make failure')
  time.sleep(2)
 else:raise TimeoutError('12 build chunks exhausted')
 r['source_after']={n:sha(b/'linux'/n)for n in r['source_before']}
 assert r['source_before']==r['source_after']
 assert sha(b/'build/.config')==r['config_before']
 shutil.copy2(b/'build/linux',b/'kernel');r['kernel_sha256']=sha(b/'kernel')
 assert (b/'kernel').read_bytes()[:4]==b'\x7fELF'
 r['build_script_sha256']=sha(b/'build.py');rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 try:
  groups={x['pid']for x in r['commands']if'pid'in x};r['groups']={str(p):group_members(p)for p in sorted(groups)};r['groups_empty']=all(x==([],[])for x in r['groups'].values())
  if not r['groups_empty']:rc=1
 except BaseException:r['cleanup_error']=traceback.format_exc();rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
sys.exit(rc)
