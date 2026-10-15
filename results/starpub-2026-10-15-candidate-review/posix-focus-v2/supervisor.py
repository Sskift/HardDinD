from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,traceback,io,tarfile
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';run=b/'run';name='focus-v2'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'preflight','pid':os.getpid(),'commands':[],'reports':{},'full_acceptance':False,'performance_acceptance':False,'sources':{str(p.relative_to(repo)):sha(p) for p in repo.rglob('*') if p.is_file() and '__pycache__' not in p.parts},'backend_sha256':sha(run/'virtiofsd')}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout=90,env=None):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb') as out,(b/(label+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=repo,stdout=out,stderr=err,stdin=subprocess.DEVNULL,start_new_session=True,env=env);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:row['timed_out']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(10)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 return row
base=['python3',str(repo/'scripts/harddind.py'),str(run),name];env={**os.environ,'LD_LIBRARY_PATH':str(b/'libs')};rc=1
os.sched_setaffinity(0,{0,1,2,3});save()
try:
 prior=json.loads((w/'gpu-mapping-owner-v13/runtime-status.json').read_text());assert prior['status']=='finished' and prior['groups_empty']
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);r['stage']='build_and_native';save()
  assert call('compile',['gcc','-O2','-Wall','-Wextra','-Werror','-static',str(repo/'src/share_probe.c'),'-o',str(run/'share-probe')])['exit_code']==0
  r['probe_sha256']=sha(run/'share-probe');save()
  cmd=['python3',str(repo/'scripts/run-posix-focus.py'),str(run),'--probe',str(run/'share-probe'),'--mode','native','--out',str(run/'results/native.json')]
  assert call('native',cmd,120)['exit_code']==0
  r['reports']['native']=json.loads((run/'results/native.json').read_text());save()
  with tarfile.open(b/'rootfs.tar','w') as tar:
   data=(run/'share-probe').read_bytes();info=tarfile.TarInfo('share-probe');info.mode=0o755;info.size=len(data);tar.addfile(info,io.BytesIO(data))
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_SH);r['stage']='docker';save()
  try:
   assert call('start',base+['start','--kernel',str(w/'cpu-cpuset-kernel-v1'),'--disk',str(w/'runtime-shares-v1.ext4'),'--share',str(run/'posix-focus-share'),'--share-cache','never','--virtiofsd',str(run/'virtiofsd'),'--share-posix-locks','--share-inline-flock'],180,env)['exit_code']==0
   assert call('import',base+['docker','import',str(b/'rootfs.tar'),'harddind-focus:local'],120,env)['exit_code']==0
   for mode in ['default','nested-enabled']:
    e={**env,'POSIX_FOCUS_ALLOW_NS':'1' if mode=='nested-enabled' else '0'}
    cmd=['python3',str(repo/'scripts/run-posix-focus.py'),str(run),'--probe',str(run/'share-probe'),'--mode','docker','--image','harddind-focus:local','--name-prefix',name,'--out',str(run/'results'/('docker-'+mode+'.json'))]
    call(mode,cmd,220,e);r['reports'][mode]=json.loads((run/'results'/('docker-'+mode+'.json')).read_text());save()
  finally:
   call('stop',base+['stop'],100,env)
   state=run/'instances'/name/'state.json'
   if state.exists():r['final_state']=json.loads(state.read_text())
 rc=0 if all(v['complete'] and not v['failures'] for v in r['reports'].values()) else 1
except BaseException:r['error']=traceback.format_exc()
finally:
 def groups(x):
  out=set()
  if isinstance(x,dict):
   if 'process_group' in x and 'clean_shutdown' in x:out.add(x['process_group'])
   for v in x.values():out|=groups(v)
  elif isinstance(x,list):
   for v in x:out|=groups(v)
  return out
 audit={str(g):group_members(g) for g in groups(r)};r['groups_empty']=all(v==([],[]) for v in audit.values());(b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
 r['inputs_unchanged']=all(sha(repo/n)==h for n,h in r['sources'].items()) and sha(run/'virtiofsd')==r['backend_sha256'];r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
