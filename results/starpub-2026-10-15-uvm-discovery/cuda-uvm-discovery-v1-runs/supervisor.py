import pathlib,subprocess,json,datetime,os
w=pathlib.Path('/root/harddind-rebuild');b=w/'results/cuda-uvm-discovery-v1-runs'
s={'status':'running','supervisor_pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runs':{}}
def save():
 (b/'status.json').write_text(json.dumps(s,indent=2))
commands=[('checks',['make','check']),('cuda-uvm-discovery-v1',['python3',str(w/'scripts/run-gpu-cuda.py'),str(w),'--kernel',str(w/'gpu-system-features-kernel-v2'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name','cuda-uvm-discovery-v1','--async-wait'])]
for name,cmd in commands:
 with (b/(name+'.log')).open('x') as log:
  p=subprocess.Popen(cmd,cwd=w,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT);s['runs'][name]={'pid':p.pid,'command':cmd};save();rc=p.wait();s['runs'][name]['exit_code']=rc;save()
 if name=='checks' and rc:break
s.update(status='finished',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
