import pathlib,subprocess,json,datetime,os
b=pathlib.Path(__file__).resolve().parent
s={'status':'running','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'supervisor_pid':os.getpid()}
def save(): (b/'status.json').write_text(json.dumps(s,indent=2)+'\n')
save()
cmd=['python3',str(b/'repo/scripts/run-cpu-limits.py'),str(b/'run'),'--kernel','/root/harddind-rebuild/gpu-system-features-kernel-v2','--disk','/root/harddind-rebuild/runtime-shares-v1.ext4','--name','cpu-limits-v1','--cpus','4']
with (b/'runtime.log').open('x') as log:
 p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,cwd=b/'repo');s['pid']=p.pid;s['command']=cmd;save();rc=p.wait()
s.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
