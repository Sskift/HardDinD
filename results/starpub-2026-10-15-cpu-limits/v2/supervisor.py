import pathlib,subprocess,json,datetime,os,time
b=pathlib.Path(__file__).resolve().parent;w=b.parent
s={'status':'running','phase':'waiting_for_cpuset_kernel','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'supervisor_pid':os.getpid()}
def save():(b/'status.json').write_text(json.dumps(s,indent=2)+'\n')
save()
try:
 deadline=time.monotonic()+2400
 while time.monotonic()<deadline:
  build=json.loads((w/'cpu-cpuset-owner-v1/status.json').read_text())
  if build['status']!='running':break
  time.sleep(2)
 else:raise RuntimeError('cpuset build did not finish in 40 minutes')
 if build.get('exit_code')!=0 or build.get('status')!='finished':raise RuntimeError('cpuset kernel build failed: '+str(build))
 s['phase']='running_docker_checks';s['kernel_sha256']=build['kernel_sha256'];save()
 cmd=['python3',str(b/'repo/scripts/run-cpu-limits.py'),str(b/'run'),'--kernel',build['kernel'],'--disk',str(w/'runtime-shares-v1.ext4'),'--name','cpu-limits-v2','--cpus','4']
 with (b/'runtime.log').open('x') as log:
  p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,cwd=b/'repo');s['pid']=p.pid;s['command']=cmd;save();s['exit_code']=p.wait()
 s['status']='finished'
except BaseException as e:s['status']='error';s['error']=repr(e)
s['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
