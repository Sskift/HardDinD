import subprocess,pathlib,json,datetime,os
b=pathlib.Path('/root/harddind-rebuild/posix-owner-v1');run=b/'run'
cmd=['python3',str(b/'repo/scripts/run-shared-locks.py'),str(run),'--kernel','/root/harddind-rebuild/gpu-system-features-kernel-v2','--disk','/root/harddind-rebuild/runtime-shares-v1.ext4','--virtiofsd',str(run/'virtiofsd'),'--name','posix-baseline-v1','--posix-locks','--inline-flock','--functional-only']
s={'status':'running','supervisor_pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'command':cmd}
def save():(b/'status.json').write_text(json.dumps(s,indent=2)+'\n')
with (b/'runtime.log').open('x') as log:
 p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT);s['pid']=p.pid;save();rc=p.wait()
s.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
