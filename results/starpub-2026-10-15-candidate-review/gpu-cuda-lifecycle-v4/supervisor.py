from pathlib import Path
import datetime,fcntl,json,os,signal,shutil,subprocess,sys,tarfile,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';candidate=repo/'lifecycle';run=b/'run';name='cuda-life-v4';sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
import harddind
sha=harddind.sha;kernel=w/'gpu-completion-owner-v1/kernel';disk=w/'runtime-shares-v1.ext4';binary=candidate/'build/cuda-lifecycle-probe';base=['python3',str(repo/'scripts/harddind.py'),str(run),name]
r={'diagnostic_only':True,'diagnostic_scope':'Only the separate docker exec observer gets internal privileged credentials to read guest task stacks. Outer capabilities/security policy and measured CUDA workload configuration remain unchanged. Diagnostic overhead precludes performance conclusions.','status':'running','stage':'waiting_publish_integration','commands':[],'phase_pass':{},'full_acceptance':False,'performance_acceptance':False,'library_sha256':json.loads((b/'input-cuda-report.json').read_text())['library_sha256']}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def acquire(mode):
 lock=(w/'kernel-build.lock').open('a');end=time.monotonic()+180
 while True:
  try:fcntl.flock(lock,mode|fcntl.LOCK_NB);return lock
  except BlockingIOError:
   if time.monotonic()>end:lock.close();raise TimeoutError('build lock wait180s')
   time.sleep(.25)
def call(label,cmd,timeout=180,env=None,strict=True):
 row={'label':label,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=candidate,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True);row['pid']=p.pid;save()
  started=time.monotonic()
  try:
   if label=='docker':
    try:row['exit_code']=p.wait(35)
    except subprocess.TimeoutExpired:
     call('diagnostic-proc',base+['docker','exec','--privileged',name,'/proc-snapshot','1'],25,strict=False)
     row['exit_code']=p.wait(max(1,timeout-(time.monotonic()-started)))
   else:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 if strict:assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],label
 return row
save();rc=1
try:
 end=time.monotonic()+1200
 while True:
  prior=json.loads((w/'gpu-cuda-lifecycle-owner-v3/owner-status.json').read_text())
  if prior['status']=='finished':break
  if time.monotonic()>end:raise TimeoutError('network integration owner queue')
  time.sleep(2)
 assert prior['groups_empty'];os.sched_setaffinity(0,{0,1,2,3});r['stage']='prepare';r['sources_before']={str(p.relative_to(candidate)):sha(p)for p in candidate.rglob('*')if p.is_file()and'build'not in p.relative_to(candidate).parts and'__pycache__'not in p.parts};save()
 assert all(sha(Path(p))==h for p,h in r['library_sha256'].items())
 with acquire(fcntl.LOCK_EX):
  call('compile',['gcc','-O2','-Wall','-Wextra','-Werror','-std=gnu11','-pthread',str(candidate/'src/cuda_cuda_lifecycle_probe.c'),'-lm','-ldl','-o',str(binary)],60)
  call('diagnostic-compile',['gcc','-O2','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-std=gnu11','-static',str(candidate/'src/proc_snapshot.c'),'-o',str(candidate/'build/proc-snapshot')],60)
  call('fixture-compile',['gcc','-O2','-Wall','-Wextra','-Werror','-std=gnu11',str(candidate/'tests/args_fixture.c'),'-lm','-ldl','-o',str(candidate/'build/cuda-lifecycle-args-fixture')],60)
  call('args-fixture',[str(candidate/'build/cuda-lifecycle-args-fixture')],30)
  call('checker-tests',['python3','-m','unittest','discover','-s','tests','-p','test_cuda_lifecycle_checks.py','-v'],120)
  shutil.copy2(w/'gpu-plain-perf-owner-v3/rootfs.tar',b/'rootfs.tar')
  with tarfile.open(b/'rootfs.tar','a')as tar:
   tar.add(binary,arcname='cuda-lifecycle-probe',recursive=False)
   tar.add(candidate/'build/proc-snapshot',arcname='proc-snapshot',recursive=False)
 r['input_sha256']={str(p):sha(p)for p in [kernel,disk,binary,b/'rootfs.tar',repo/'scripts/harddind.py',w/'network-backend',w/'client/docker',w/'client/ld-musl-x86_64.so.1']};save()
 args=['--gpus','0,1,2,3','--iterations','8','--thread-cycles','4','--child-cycles','2','--threads','4','--children','4','--elements','262144','--deadline-ms','30000']
 for phase in ['native-before','docker','native-after']:
  r['stage']=phase;save()
  with acquire(fcntl.LOCK_SH if phase=='docker'else fcntl.LOCK_EX):
   try:
    if phase=='native-before':
     call('inventory',['nvidia-smi','--query-gpu=uuid','--format=csv,noheader,nounits'],30)
     uuids=(b/'inventory.stdout').read_text().strip().splitlines();assert len(uuids)==len(set(uuids))==4 and all(x.startswith('GPU-')for x in uuids);r['expected_uuids']=uuids;save()
    if phase=='docker':
     call('start',base+['start','--kernel',str(kernel),'--disk',str(disk),'--gpu-control-experiment','--gpu-async-wait','--memfd-ram','--memfd-stub'])
     r['ready_state']=harddind.read_state(run/'instances'/name);save();call('import',base+['docker','import',str(b/'rootfs.tar'),'harddind-cuda-lifecycle:local'])
    cache=b/('cache-'+phase);cache.mkdir();env={**os.environ,'CUDA_CACHE_PATH':str(cache)};env.pop('LD_PRELOAD',None)
    devices=['--device=/dev/nvidiactl:/dev/nvidiactl']+[f'--device=/dev/nvidia{i}:/dev/nvidia{i}'for i in range(4)]+['--device=/dev/nvidia-uvm:/dev/nvidia-uvm']
    cmd=[str(binary),*args]if phase!='docker'else base+['docker','run','--rm','--name','cuda-life-v4',*devices,'--env=CUDA_CACHE_PATH=/tmp/cuda-cache','harddind-cuda-lifecycle:local','/cuda-lifecycle-probe',*args]
    actual=call(phase,cmd,180,env,False)
    checker=['python3',str(candidate/'scripts/cuda_lifecycle_checks.py'),str(b/(phase+'.stdout')),'--uuids',','.join(r['expected_uuids'])]
    if phase!='native-before':checker+=['--native-reference',str(b/'native-before.stdout')]
    check=call(phase+'-check',checker,30,strict=False)
    r['phase_pass'][phase]=actual['exit_code']==0 and check['exit_code']==0 and not actual.get('timeout') and actual['cleanup']['clean_shutdown'];save()
    if phase=='native-before':assert r['phase_pass'][phase],'native baseline calibration failed'
   finally:
    if phase=='docker':
     call('remaining-container',base+['docker','ps','-aq','--filter','name=^/cuda-life-v3$'],30)
     if(b/'remaining-container.stdout').read_text().strip():call('remove',base+['docker','rm','-f','cuda-life-v4'],30)
     call('stop',base+['stop']);r['final_state']=harddind.read_state(run/'instances'/name);save()
 rc=0 if len(r['phase_pass'])==3 and all(r['phase_pass'].values())else 1
except BaseException:r['error']=traceback.format_exc()
finally:
 try:
  st=harddind.read_state(run/'instances'/name)
  if harddind.live(st):call('final-stop',base+['stop']);st=harddind.read_state(run/'instances'/name)
  r['final_state']=st;r['normal_shutdown']=st.get('phase')=='stopped'and st.get('guest_exit_code')==0 and not st.get('forced_shutdown',True)
  groups={c['pid']for c in r['commands']if'pid'in c};groups.update(st.get(k)for k in ['guest_pid','network_pid','supervisor_pid']if st.get(k))
  r['groups']={str(g):group_members(g)for g in sorted(groups)};r['groups_empty']=all(v==([],[])for v in r['groups'].values())
  r['sources_after']={n:sha(candidate/n)for n in r.get('sources_before',{})};r['inputs_unchanged']=bool(r.get('input_sha256'))and r.get('sources_before')==r['sources_after']and all(sha(Path(p))==h for p,h in r['input_sha256'].items())and all(sha(Path(p))==h for p,h in r['library_sha256'].items())
  if not(r['normal_shutdown']and r['groups_empty']and r['inputs_unchanged']):rc=1
 except BaseException:r['cleanup_error']=traceback.format_exc();rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
