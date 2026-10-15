from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'wait_gpu_owner','commands':[],'full_acceptance':False,'nested_docker_test':False}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def snapshot(root):return {str(p.relative_to(root)):sha(p) for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
def call(label,cmd,env,fd,timeout=180):
 row={'label':label,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=repo/'tests/publish',stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,pass_fds=(fd,),start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and row['cleanup']['clean_shutdown'] and not row.get('timeout'),label
save();rc=1
try:
 until=time.monotonic()+1800
 while True:
  p=w/'gpu-plain-perf-owner-v3/owner-status.json'
  prior=json.loads(p.read_text()) if p.exists() else {}
  if prior.get('status')=='finished':break
  if time.monotonic()>until:raise TimeoutError('owner queue wait')
  time.sleep(2)
 assert prior['groups_empty'];r['prior_gpu_exit_code']=prior['exit_code'];r['stage']='build_and_native';r['source_before']=snapshot(repo);r['independent_before']=snapshot(b/'independent/tests');save()
 os.sched_setaffinity(0,{0,1,2,3})
 with (w/'kernel-build.lock').open('a')as lock:
  until=time.monotonic()+180
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>until:raise TimeoutError('global lock wait 180s')
    time.sleep(.25)
  env={**os.environ,'HARDDIND_BUILD_LOCK_FD':str(lock.fileno()),'HARDDIND_LOCK_WAIT':'180','ASAN_OPTIONS':'detect_leaks=0:abort_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
  for variant in ['release','asan']:
   buildcmd=['bash',str(repo/'scripts/build-network.sh'),str(b/'build')]+(['asan']if variant=='asan'else[])
   call(variant+'-build',buildcmd,env,lock.fileno(),240)
   src=b/'build/libslirp-stage/libslirp-v4.6.1';out=b/variant;out.mkdir()
   srcsha=snapshot(src/'src');(b/(variant+'-source-sha256.json')).write_text(json.dumps(srcsha,indent=2)+'\n')
   if variant=='release':shutil_marker=None
   env2={**env,**({'FRAG_SANITIZE':'asan'}if variant=='asan'else{})}
   call(variant+'-independent-build',['bash',str(b/'independent/tests/build_and_link.sh'),str(src),str(out/'slirp.a'),str(out/'frag-repro')],env2,lock.fileno(),240)
   call(variant+'-independent-tests',[str(out/'frag-repro')],env2,lock.fileno(),30)
   backend=b/'build'/('network-backend-asan'if variant=='asan'else'network-backend')
   r.setdefault('backend_sha256',{})[variant]=sha(backend);save()
   call(variant+'-backend-tests',['python3','-m','unittest','discover','-s','.','-p','test_*.py','-v'],{**env2,'HARDDIND_NETWORK_BACKEND':str(backend)},lock.fileno(),240)
  r['source_after']=snapshot(repo);r['independent_after']=snapshot(b/'independent/tests');r['inputs_unchanged']=r['source_before']==r['source_after'] and r['independent_before']==r['independent_after'];assert r['inputs_unchanged'];rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 r['groups']={str(c['pid']):group_members(c['pid'])for c in r['commands']if'pid'in c};r['groups_empty']=all(v==([],[])for v in r['groups'].values());r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
