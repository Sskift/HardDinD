from pathlib import Path
import subprocess,sys,json,datetime,os
w=Path('/root/harddind-rebuild');o=w/'results/gpu-fixed-alloc-v1-runs'
state={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runs':{}}
def save(): (o/'status.json').write_text(json.dumps(state,indent=2)+'\n')
save()
commands=[('docker',['python3',str(w/'scripts/run-gpu-control-docker.py'),str(w),'--kernel',str(w/'gpu-fixed-alloc-kernel-v1'),'--disk',str(w/'runtime-shares-v1.ext4'),'--probe',str(w/'results/gpu-alloc-boundary-v1/gpu-guest-probe'),'--native-baseline',str(w/'results/gpu-alloc-boundary-v1/report.json'),'--name','gpu-alloc-docker-v1','--alloc-object','--async-wait']),('cuda',['python3',str(w/'scripts/run-gpu-cuda.py'),str(w),'--kernel',str(w/'gpu-fixed-alloc-kernel-v1'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name','cuda-fixed-alloc-v1','--async-wait']),('policy-regression',['python3',str(w/'scripts/run-gpu-control.py'),str(w),'--kernel',str(w/'gpu-fixed-alloc-kernel-v1'),'--name','gpu-policy-alloc-regression-v1','--share-policy','--async-wait']),('check',['make','check'])]
try:
 for name,cmd in commands:
  with (o/(name+'.log')).open('x') as log:
   p=subprocess.Popen(cmd,cwd=w,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
   state['runs'][name]={'pid':p.pid,'command':cmd};save();state['runs'][name]['exit_code']=p.wait();save()
  if name=='docker':
   report=json.loads((w/'results/gpu-alloc-docker-v1/report.json').read_text())
   if report.get('checks',{}).get('normal_shutdown')!='pass':raise RuntimeError('docker did not confirm clean shutdown; skip later GPU use')
 state['status']='finished'
except BaseException as e:state['status']='error';state['error']=repr(e)
state['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
