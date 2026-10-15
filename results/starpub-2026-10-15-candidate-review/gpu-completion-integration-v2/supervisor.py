from pathlib import Path
import json,os,fcntl,subprocess,shutil,sys,signal,traceback,time,hashlib,tarfile
b=Path(__file__).resolve().parent;w=b.parent;parent=w/'gpu-completion-owner-v1';repo=b/'repo';run=b/'run';kernel=parent/'kernel'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_matrix','commands':[],'full_acceptance':False}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout=900,cwd=None):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=cwd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGINT);row['exit_code']=p.wait(30)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and row['cleanup']['clean_shutdown'],label
 return row
save();rc=1
try:
 deadline=time.monotonic()+1800
 while True:
  prior_replay=json.loads((w/'gpu-completion-integration-v1/owner-status.json').read_text())
  if prior_replay['status']=='finished':break
  if time.monotonic()>deadline:raise TimeoutError('prior replay')
  time.sleep(2)
 assert prior_replay['groups_empty']
 r['prior_replay_status']={k:prior_replay.get(k)for k in ['exit_code','exact_match','error']};save()
 while True:
  prior=json.loads((parent/'owner-status.json').read_text())
  if prior['status']=='finished':break
  if time.monotonic()>deadline:raise TimeoutError('prior GPU matrix')
  time.sleep(2)
 assert prior['groups_empty'] and prior['normal_shutdown'] and all(c['all_four_verified']for c in prior['cases'])
 os.sched_setaffinity(0,{0,1,2,3});r['source_inputs']={str(p.relative_to(repo)):sha(p)for p in repo.rglob('*')if p.is_file()and'__pycache__'not in p.parts};r['kernel_sha256']=sha(kernel);save()
 with(w/'kernel-build.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  r['stage']='full_series_reapply';save();expected=json.loads((parent/'build-status.json').read_text())['source_after'];source=b/'linux-7.2.10';source.mkdir();archive=w/'cache/linux-7.2.10.tar.xz'
  with tarfile.open(archive,'r|*')as tar:
   for member in tar:
    name=member.name.removeprefix('linux-7.2.10/')
    if name in expected and member.isfile():
     p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(tar.extractfile(member).read())
  call('mapping-series',['python3',str(repo/'scripts/apply-kernel-patches.py'),str(source),str(archive),'--gpu-mapping-experiment'],180)
  r['actual_files']={n:sha(source/n)for n in expected};r['expected_files']=expected;r['exact_match']=r['actual_files']==expected;assert r['exact_match']
  # The earlier async target must also apply without the mapping experiment.
  asyncsrc=b/'async/linux-7.2.10';asyncsrc.mkdir(parents=True)
  with tarfile.open(archive,'r|*')as tar:
   for member in tar:
    name=member.name.removeprefix('linux-7.2.10/')
    if name in expected and member.isfile():
     p=asyncsrc/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(tar.extractfile(member).read())
  call('async-series',['python3',str(repo/'scripts/apply-kernel-patches.py'),str(asyncsrc),str(archive),'--gpu-async-experiment'],180)
  r['async_series_applied']=True
  obj=w/'arkclaude/round5/gpu-mapping-repair/.work/build/arch/um/kernel/skas/stub.o';shutil.copy2(obj,b/'stub.o');r['stub_object_sha256']=sha(b/'stub.o')
  call('stub-relocations',['python3',str(repo/'scripts/check-stub-relocations.py'),str(b/'stub.o')],30)
 run.mkdir();(run/'tmp').mkdir();(run/'linux-7.2.10').mkdir();shutil.copy2(source/'.harddind-patches.json',run/'linux-7.2.10/.harddind-patches.json')
 for n in ['kernel-build.lock','probe-root']:(run/n).symlink_to(w/n)
 r['stage']='control_regressions';save()
 for label,mode in [('completion-features','--system-features'),('completion-alloc','--alloc-object')]:
  call(label,['python3',str(repo/'scripts/run-gpu-control.py'),str(run),'--kernel',str(kernel),'--name',label,mode,'--async-wait','--memfd-ram','--memfd-stub'])
  report=json.loads((run/'results'/label/'report.json').read_text());r[label]={'diagnostic_complete':report['diagnostic_complete'],'phases':{k:v['status']for k,v in report['phases'].items()}};assert report['diagnostic_complete'];save()
 rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 def groups(v):
  out=set()
  if isinstance(v,dict):
   if'process_group'in v:out.add(v['process_group'])
   for x in v.values():out.update(groups(x))
  elif isinstance(v,list):
   for x in v:out.update(groups(x))
  return out
 gs=groups(r)
 for p in (run/'results').glob('*/report.json'):gs.update(groups(json.loads(p.read_text())))
 r['groups']={str(g):group_members(g)for g in gs};r['groups_empty']=all(x==([],[])for x in r['groups'].values());r['inputs_unchanged']=all(sha(repo/n)==h for n,h in r.get('source_inputs',{}).items())and sha(kernel)==r.get('kernel_sha256')
 if not r['groups_empty']or not r['inputs_unchanged']:rc=1
 r.update(status='finished',exit_code=rc);save()
