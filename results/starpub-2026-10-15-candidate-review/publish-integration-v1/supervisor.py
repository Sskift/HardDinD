from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'prepare','commands':[],'full_acceptance':False,'performance_acceptance':False}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def snapshot():return {str(p.relative_to(repo)):sha(p)for p in repo.rglob('*')if p.is_file()and '__pycache__'not in p.parts}
def call(label,cmd,env,fd,timeout=240):
 row={'label':label,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=repo,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,pass_fds=(fd,),start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and row['cleanup']['clean_shutdown']and not row.get('timeout'),label
save();rc=1
try:
 gate=w/'publish-fragment-docker-owner-v1/post-run-audit.json';prior=json.loads(gate.read_text());assert prior['audit_passed']and prior['functional_subset_passed'];r['docker_gate_sha256']=sha(gate)
 os.sched_setaffinity(0,{0,1,2,3});r['source_before']=snapshot();r['stage']='build_and_check';save()
 with(w/'kernel-build.lock').open('a')as lock:
  deadline=time.monotonic()+180
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>deadline:raise TimeoutError('global lock wait 180s')
    time.sleep(.25)
  env={**os.environ,'HARDDIND_BUILD_LOCK_FD':str(lock.fileno()),'HARDDIND_NETWORK_BACKEND':str(b/'build/network-backend'),'JOBS':'4','GOMAXPROCS':'4'}
  call('network-build',['bash',str(repo/'scripts/build-network.sh'),str(b/'build')],env,lock.fileno())
  assert(b/'build/network-probe').exists();r['binary_sha256']={p.name:sha(p)for p in[b/'build/network-backend',b/'build/network-probe']};save()
  call('main-check',['make','check'],env,lock.fileno())
  call('backend-tests',['python3','-m','unittest','discover','-s','tests/publish','-v'],env,lock.fileno())
  call('probe-addresses',[str(b/'build/network-probe'),'addresses','lo'],env,lock.fileno(),30)
  raw=(b/'probe-addresses.stdout').read_text();assert'4 127.0.0.1\n'in raw and'6 ::1\n'in raw
  r['source_after']=snapshot();r['source_unchanged']=r['source_after']==r['source_before'];assert r['source_unchanged'];rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 r['groups']={str(x['pid']):group_members(x['pid'])for x in r['commands']if'pid'in x};r['groups_empty']=all(v==([],[])for v in r['groups'].values());r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
