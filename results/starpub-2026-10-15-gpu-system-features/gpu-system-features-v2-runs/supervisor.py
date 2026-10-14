from pathlib import Path
import subprocess,datetime,os,json
w=Path('/root/harddind-rebuild');o=w/'results/gpu-system-features-v2-runs'
state={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runs':{}}
def save():(o/'status.json').write_text(json.dumps(state,indent=2)+'\n')
save();k=str(w/'gpu-system-features-kernel-v2');disk=str(w/'runtime-shares-v1.ext4')
cmds=[]
for name,mode in [('gpu-system-features-v2','--system-features'),('gpu-alloc-finn-v3','--alloc-object')]:
 cmds.append((name,['python3',str(w/'scripts/run-gpu-control.py'),str(w),'--kernel',k,'--name',name,mode,'--async-wait']))
for name,base,mode in [('gpu-features-docker-v2','gpu-system-features-v2','--system-features'),('gpu-alloc-finn-docker-v3','gpu-alloc-finn-v3','--alloc-object')]:
 cmds.append((name,['python3',str(w/'scripts/run-gpu-control-docker.py'),str(w),'--kernel',k,'--disk',disk,'--probe',str(w/'results'/base/'gpu-guest-probe'),'--native-baseline',str(w/'results'/base/'report.json'),'--name',name,mode,'--async-wait']))
cmds.append(('cuda-system-features-v2',['python3',str(w/'scripts/run-gpu-cuda.py'),str(w),'--kernel',k,'--disk',disk,'--name','cuda-system-features-v2','--async-wait']))
cmds.append(('checks',['make','check']))
try:
 for name,cmd in cmds:
  with (o/(name+'.log')).open('x') as log:
   p=subprocess.Popen(cmd,cwd=w,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
   state['runs'][name]={'pid':p.pid,'command':cmd};save();state['runs'][name]['exit_code']=p.wait();save()
  if name.startswith('gpu-'):
   report=json.loads((w/'results'/name/'report.json').read_text())
   if 'docker' in name:
    if report.get('checks',{}).get('normal_shutdown')!='pass':raise RuntimeError(name+' did not confirm clean shutdown')
   else:
    if not report.get('diagnostic_finished') or not all(v.get('cleanup',{}).get('clean_shutdown') for v in report.get('phases',{}).values()):raise RuntimeError(name+' did not confirm complete observation and clean shutdown')
 state['status']='finished'
except BaseException as e:state['status']='error';state['error']=repr(e)
state['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
