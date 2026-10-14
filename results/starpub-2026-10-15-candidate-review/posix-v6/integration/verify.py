from pathlib import Path
import subprocess,json,hashlib,fcntl,os,time,sys,signal
b=Path(__file__).resolve().parent;sys.path.insert(0,str(b/'scripts'));from guest_cleanup import cleanup_group,group_members
os.sched_setaffinity(0,{0,1,2,3});r={'status':'running','pid':os.getpid(),'sha256':{str(p.relative_to(b)):hashlib.sha256(p.read_bytes()).hexdigest() for d in ['src','scripts','tests','guest','docs','virtiofs'] for p in (b/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts}}
def save():(b/'report.json').write_text(json.dumps(r,indent=2)+'\n')
save()
with (b.parent/'kernel-build.lock').open('a') as lock:
 end=time.monotonic()+1200
 while True:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
  except BlockingIOError:
   if time.monotonic()>end:raise RuntimeError('lock wait expired')
   time.sleep(1)
 with (b/'check.stdout').open('xb') as o,(b/'check.stderr').open('xb') as e:
  p=subprocess.Popen(['make','check'],cwd=b,stdout=o,stderr=e,stdin=subprocess.DEVNULL,start_new_session=True)
  try:rc=p.wait(timeout=180)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);rc=p.wait(timeout=15);r['timed_out']=True
  finally:r['cleanup']=cleanup_group(p.pid)
 r['exit_code']=rc;r['pid_check']=p.pid
r['groups_empty']=group_members(p.pid)==([],[])
r['inputs_unchanged']=all(hashlib.sha256((b/n).read_bytes()).hexdigest()==h for n,h in r['sha256'].items())
r['status']='finished';save()
