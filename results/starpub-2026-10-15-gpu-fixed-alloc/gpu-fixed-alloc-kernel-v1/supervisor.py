from pathlib import Path
import datetime, hashlib, json, os, shutil, subprocess
w=Path('/root/harddind-rebuild');o=w/'results/gpu-fixed-alloc-kernel-v1'
state={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save(): (o/'status.json').write_text(json.dumps(state,indent=2)+'\n')
save()
try:
 with (o/'build.log').open('x') as log:
  p=subprocess.Popen(['bash',str(w/'scripts/build-kernel.sh'),str(w)],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'GPU_ASYNC_EXPERIMENT':'1','JOBS':'4'})
  state['build_pid']=p.pid;save();state['exit_code']=p.wait()
 if state['exit_code']==0:
  kernel=w/'gpu-fixed-alloc-kernel-v1';assert not kernel.exists();shutil.copy2(w/'thread-build/linux',kernel)
  state['kernel']=str(kernel);state['kernel_sha256']=hashlib.sha256(kernel.read_bytes()).hexdigest()
  shutil.copy2(w/'linux-7.2.10/.harddind-patches.json',o/'kernel-patches.json')
  shutil.copy2(w/'thread-build/.config',o/'kernel.config')
  for name in ['0023-um-gpu-client-policy.patch','0025-um-gpu-policy-driver-validation.patch','0026-um-gpu-fixed-alloc.patch']:shutil.copy2(w/'kernel/experiments'/name,o/name)
 state['status']='finished'
except BaseException as e:state['status']='error';state['error']=repr(e)
state['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
