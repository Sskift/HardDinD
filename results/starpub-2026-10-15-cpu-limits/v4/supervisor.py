import pathlib,subprocess,json,datetime,os
b=pathlib.Path(__file__).resolve().parent
s={'status':'running','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'supervisor_pid':os.getpid()}
def save(): (b/'status.json').write_text(json.dumps(s,indent=2)+'\n')
save()
with (b/'validation.log').open('x') as log:
 validation=['flock','-x','/root/harddind-rebuild/kernel-build.lock','timeout','--kill-after=10s','90','python3','-m','unittest','discover','-s','tests']
 p=subprocess.Popen(validation,stdout=log,stderr=subprocess.STDOUT,cwd=b/'repo');s['validation_pid']=p.pid;save();rc=p.wait()
 s['validation_exit_code']=rc;save()
 if rc:
  s.update(status='finished',exit_code=rc,failed_step='validation',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save();raise SystemExit(rc)
cmd=['python3',str(b/'repo/scripts/run-cpu-limits.py'),str(b/'run'),'--kernel','/root/harddind-rebuild/cpu-cpuset-kernel-v1','--disk','/root/harddind-rebuild/runtime-shares-v1.ext4','--name','cpu-limits-v4','--cpus','4']
with (b/'runtime.log').open('x') as log:
 p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,cwd=b/'repo');s['pid']=p.pid;s['command']=cmd;save();rc=p.wait()
s.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
