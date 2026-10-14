import pathlib,subprocess,time,json,shutil,os,datetime
w=pathlib.Path('/root/harddind-rebuild');stage=w/'gpu-system-features-v2-stage';out=w/'results/gpu-system-features-kernel-v2'
state={'pid':os.getpid(),'status':'waiting_for_v1','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save():(out/'controller.json').write_text(json.dumps(state,indent=2)+'\n')
save()
try:
 deadline=time.monotonic()+3600
 while True:
  old=json.loads((w/'results/gpu-system-features-v1-runs/status.json').read_text())
  if old['status']!='running':break
  if time.monotonic()>deadline:raise RuntimeError('v1 still running; leave it intact, do not build')
  time.sleep(5)
 if old['status']!='finished':raise RuntimeError('v1 supervisor did not finish normally')
 for n in ['gpu-features-docker-v1','gpu-alloc-finn-docker-v2']:
  d=json.loads((w/'results'/n/'report.json').read_text());assert d['checks']['normal_shutdown']=='pass'
 d=json.loads((w/'results/cuda-system-features-v1/report.json').read_text());assert d['normal_shutdown']
 shutil.copy2(stage/'0027-um-gpu-system-features.patch',w/'kernel/experiments/0027-um-gpu-system-features.patch')
 state['status']='building';save()
 subprocess.run(['python3',str(out/'build.py')],check=True)
 built=json.loads((out/'status.json').read_text());assert built.get('exit_code')==0 and built.get('kernel_sha256')
 state['status']='testing';save()
 ro=w/'results/gpu-system-features-v2-runs';ro.mkdir();shutil.copy2(stage/'runs.py',ro/'supervisor.py')
 with (ro/'supervisor.log').open('x') as log:
  p=subprocess.Popen(['python3',str(ro/'supervisor.py')],cwd=w,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
  state['runs_pid']=p.pid;save();p.wait()
 state['runtime_status']=json.loads((ro/'status.json').read_text())['status'];state['status']='finished'
except BaseException as e:state['status']='error';state['error']=repr(e)
state['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
