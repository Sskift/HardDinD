from pathlib import Path
import subprocess,json,hashlib,fcntl,os,time,sys
b=Path(__file__).resolve().parent;sys.path.insert(0,str(b/'scripts'));from guest_cleanup import cleanup_group
os.sched_setaffinity(0,{0,1,2,3})
r={'status':'running','pid':os.getpid(),'source_sha256':{str(p.relative_to(b)):hashlib.sha256(p.read_bytes()).hexdigest() for d in ['src','scripts','tests','docs'] for p in (b/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts},'commands':[]}
def save():(b/'report.json').write_text(json.dumps(r,indent=2)+'\n')
save()
with (b.parent/'kernel-build.lock').open('a') as lock:
 end=time.monotonic()+1200
 while True:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
  except BlockingIOError:
   if time.monotonic()>end:raise RuntimeError('lock wait expired')
   time.sleep(1)
 for label,cmd in [('syntax',['python3','-m','py_compile','scripts/run-gpu-cuda.py','scripts/gpu_lifecycle_checks.py']),('help',['python3','scripts/run-gpu-cuda.py','--help']),('checker-tests',['python3','-m','unittest','discover','-s','tests','-p','test_gpu_lifecycle_checks.py','-v'])]:
  with (b/(label+'.stdout')).open('xb') as o,(b/(label+'.stderr')).open('xb') as e:
   p=subprocess.Popen(cmd,cwd=b,stdin=subprocess.DEVNULL,stdout=o,stderr=e,start_new_session=True)
   try:rc=p.wait(timeout=60)
   finally:cleanup=cleanup_group(p.pid)
  r['commands'].append({'label':label,'command':cmd,'pid':p.pid,'exit_code':rc,'cleanup':cleanup});save();assert rc==0 and cleanup['clean_shutdown'],label
 assert 'skipped=' not in (b/'checker-tests.stderr').read_text()
 assert 'Ran 66 tests' in (b/'checker-tests.stderr').read_text()
r['status']='passed';save()
