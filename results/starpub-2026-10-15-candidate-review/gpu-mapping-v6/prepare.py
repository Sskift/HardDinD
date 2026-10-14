from pathlib import Path
import datetime, fcntl, hashlib, json, os, shutil, signal, subprocess, sys, traceback
b=Path(__file__).resolve().parent;w=b.parent;a=w/'arkclaude/round5/gpu-mapping-repair';src=a/'.work/linux-7.2.10';build=a/'.work/build';base=w/'gpu-mapping-owner-baseline-v4';prev=w/'gpu-mapping-owner-v4'
sys.path.insert(0,str(prev/'repo/scripts'))
from guest_cleanup import cleanup_group
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_lock','pid':os.getpid(),'full_acceptance':False,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save():(b/'preparation-status.json').write_text(json.dumps(r,indent=2)+'\n')
save()
try:
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  assert (build/'exit-code3.txt').read_text().strip()=='BUILD_EXIT=0'
  targets=sorted(str(p.relative_to(base/'source')) for p in (base/'source').rglob('*') if p.is_file())
  stamped=json.loads((base/'baseline-patches.json').read_text())
  tracked=sorted(set(targets)|set(stamped['files']))
  before={n:sha(src/n) for n in tracked}
  for n,h in stamped['files'].items():
   if n not in targets:assert before[n]==h, 'unexpected baseline change '+n
  r.update(stage='owner_rebuild',source_sha256_before=before);save()
  for n in targets:
   p=b/'source'/n;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src/n,p)
  cmd=['make','-C',str(src),'O='+str(build),'ARCH=um','-j4']
  for n in targets:cmd.extend(['-W',str(src/n)])
  with (b/'owner-rebuild.log').open('xb') as out:
   p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
   r['build_command']=cmd;r['build_pid']=p.pid;save()
   try:code=p.wait(timeout=360)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=15);raise
   finally:r['build_cleanup']=cleanup_group(p.pid);save()
  assert code==0 and r['build_cleanup']['clean_shutdown'], 'owner rebuild failed'
  r['source_sha256_after']={n:sha(src/n) for n in tracked}
  assert before==r['source_sha256_after'],'source changed during rebuild'
  shutil.copy2(build/'linux',b/'kernel');shutil.copy2(build/'.config',b/'kernel.config')
  shutil.copy2(build/'build0024d.log',b/'author-build.log');shutil.copy2(base/'baseline-patches.json',b/'baseline-patches.json')
  patch=bytearray()
  for n in targets:
   q=subprocess.run(['diff','-u','--label','a/'+n,'--label','b/'+n,str(base/'source'/n),str(b/'source'/n)],capture_output=True)
   assert q.returncode in (0,1) and not q.stderr
   patch.extend(q.stdout)
  (b/'0024-um-gpu-mapping-lifecycle.patch').write_bytes(patch)
  shutil.copytree(base/'source',b/'reapplied')
  q=subprocess.run(['patch','--batch','--forward','--fuzz=0','-p1','-i',str(b/'0024-um-gpu-mapping-lifecycle.patch')],cwd=b/'reapplied',capture_output=True)
  (b/'patch-apply.stdout').write_bytes(q.stdout);(b/'patch-apply.stderr').write_bytes(q.stderr)
  assert q.returncode==0 and all((b/'source'/n).read_bytes()==(b/'reapplied'/n).read_bytes() for n in targets),'exact patch reapply failed'
  r['kernel_sha256']=sha(b/'kernel');r['patch_sha256']=sha(b/'0024-um-gpu-mapping-lifecycle.patch')
  (b/'manifest.json').write_text(json.dumps({str(p.relative_to(b)):sha(p) for p in b.rglob('*') if p.is_file() and p.name!='preparation-status.json'},indent=2)+'\n')
  shutil.copytree(prev/'repo',b/'repo',ignore=shutil.ignore_patterns('__pycache__'))
  shutil.copy2(b/'0024-um-gpu-mapping-lifecycle.patch',b/'repo/kernel/experiments/0024-um-gpu-mapping-lifecycle.patch')
  run=b/'run';run.mkdir();(run/'tmp').mkdir();(run/'linux-7.2.10').mkdir()
  for n in ['kernel-build.lock','network-backend','client','probe-root']:(run/n).symlink_to(w/n)
  stamp=json.loads((base/'baseline-patches.json').read_text());stamp['patches']['0024-um-gpu-mapping-lifecycle.patch']=r['patch_sha256']
  stamp['files'].update({n:sha(b/'source'/n) for n in targets})
  (run/'linux-7.2.10/.harddind-patches.json').write_text(json.dumps(stamp,indent=2)+'\n')
  (b/'runtime.py').write_text((prev/'runtime.py').read_text().replace('mapping-features-v4','mapping-features-v6').replace('mapping-alloc-v4','mapping-alloc-v6').replace('mapping-cuda-v4','mapping-cuda-v6'))
  (b/'runtime-inputs.json').write_text(json.dumps({str(p.relative_to(b)):sha(p) for p in (b/'repo').rglob('*') if p.is_file()},indent=2)+'\n')
  r.update(stage='ready',status='finished',exit_code=0,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
 with (b/'runtime-controller.log').open('xb') as out:
  p=subprocess.Popen(['python3',str(b/'runtime.py')],stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
 (b/'runtime-launch.json').write_text(json.dumps({'pid':p.pid})+'\n')
except BaseException:
 r.update(status='failed',exit_code=1,error=traceback.format_exc(),finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save();raise
