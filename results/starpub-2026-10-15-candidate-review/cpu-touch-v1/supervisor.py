from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,shutil,statistics,subprocess,sys,tarfile,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';run=b/'run';name='cpu-touch-v1';sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
import harddind
sha=harddind.sha;kernel=w/'gpu-completion-owner-v1/kernel';disk=w/'runtime-shares-v1.ext4';base=['python3',str(repo/'scripts/harddind.py'),str(run),name];inputs=json.loads((b/'input-cuda-report.json').read_text())
orders=['repeat0','repeat1','repeat2'];fields=['cold_ns','warm_ns','cold_minor','warm_minor','cold_major','warm_major']
r={'status':'running','stage':'prepare','commands':[],'samples':{},'full_acceptance':False,'performance_acceptance':False,'workload':'one byte per page cold and warm, fresh mmap each iteration','orders':orders,'library_sha256':inputs['library_sha256'],'host_affinity':[0,1,2,3]}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def acquire(mode):
 f=(w/'kernel-build.lock').open('a');end=time.monotonic()+180
 while True:
  try:fcntl.flock(f,mode|fcntl.LOCK_NB);return f
  except BlockingIOError:
   if time.monotonic()>end:f.close();raise TimeoutError('global lock wait180s')
   time.sleep(.25)
def call(label,cmd,timeout=180,env=None):
 row={'label':label,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save();t=time.monotonic()
 with(b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['wall_seconds']=time.monotonic()-t;row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],label
 return row
save();rc=1
try:
 prior=json.loads((w/'fp-matrix-owner-v4/owner-status.json').read_text());assert prior['status']=='finished' and prior['groups_empty'];os.sched_setaffinity(0,{0,1,2,3})
 assert all(sha(Path(p))==h for p,h in r['library_sha256'].items())
 source=repo/'src/cpu_touch_probe.c';r['probe_source_sha256']=sha(source)
 with acquire(fcntl.LOCK_EX):
  call('compile',['gcc','-O2','-Wall','-Wextra','-Werror',str(source),'-o',str(b/'cpu-touch-probe'),'-static'],60)
  shutil.copy2(w/'gpu-plain-perf-owner-v3/rootfs.tar',b/'rootfs.tar')
  with tarfile.open(b/'rootfs.tar','a')as tar:tar.add(b/'cpu-touch-probe',arcname='cpu-touch-probe',recursive=False)
 r['input_sha256']={str(p):sha(p)for p in [kernel,disk,source,b/'cpu-touch-probe',b/'rootfs.tar',repo/'scripts/harddind.py',w/'network-backend',w/'client/docker',w/'client/ld-musl-x86_64.so.1']};save()
 for phase in ['native-before','docker','native-after']:
  r['stage']=phase;save()
  with acquire(fcntl.LOCK_SH if phase=='docker'else fcntl.LOCK_EX):
   try:
    if phase=='docker':
     call('start',base+['start','--kernel',str(kernel),'--disk',str(disk),'--gpu-control-experiment','--gpu-async-wait','--memfd-ram','--memfd-stub'])
     r['ready_state']=harddind.read_state(run/'instances'/name);save();call('import',base+['docker','import',str(b/'rootfs.tar'),'harddind-cpu-touch:local'])
    r['samples'][phase]=[]
    for seq,order in enumerate(orders):
     label=phase+'-'+str(seq);cache=b/('cache-'+label);cache.mkdir();env={**os.environ,'CUDA_CACHE_PATH':str(cache)};env.pop('LD_PRELOAD',None)
     cmd=[str(b/'cpu-touch-probe')]if phase!='docker'else base+['docker','run','--rm','--name','cpu-touch-'+str(seq),'harddind-cpu-touch:local','/cpu-touch-probe']
     row=call(label,cmd,180,env);records=[json.loads(line)for line in(b/(label+'.stdout')).read_text().splitlines()]
     assert len(records)==24 and not(b/(label+'.stderr')).read_bytes()
     assert [(x['bytes'],x['iteration'])for x in records]==[(size,it)for size in [1048576,4194304,16777216]for it in range(8)]
     assert all(x['verified']is True and x['pages']*x['page_size']==x['bytes'] and all(type(x[f])is int and x[f]>=0 for f in fields)for x in records)
     r['samples'][phase].append({'seq':seq,'order':order,'records':records,'command_wall_seconds':row['wall_seconds']});save()
   finally:
    if phase=='docker':call('stop',base+['stop']);r['final_state']=harddind.read_state(run/'instances'/name);save()
 r['summary']={phase:{str(size):{f:{'median':statistics.median(x[f]for sample in samples for x in sample['records']if x['bytes']==size),'raw':[x[f]for sample in samples for x in sample['records']if x['bytes']==size]}for f in fields}for size in [1048576,4194304,16777216]}for phase,samples in r['samples'].items()}
 rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 try:
  st=harddind.read_state(run/'instances'/name)
  if harddind.live(st):call('final-stop',base+['stop']);st=harddind.read_state(run/'instances'/name)
  r['final_state']=st;r['normal_shutdown']=st.get('phase')=='stopped'and st.get('guest_exit_code')==0 and not st.get('forced_shutdown',True)
  groups={c['pid']for c in r['commands']if'pid'in c};groups.update(st.get(k)for k in ['guest_pid','network_pid','supervisor_pid']if st.get(k))
  r['groups']={str(g):group_members(g)for g in sorted(groups)};r['groups_empty']=all(v==([],[])for v in r['groups'].values())
  r['inputs_unchanged']=bool(r.get('input_sha256'))and all(sha(Path(p))==h for p,h in r['input_sha256'].items())and all(sha(Path(p))==h for p,h in r['library_sha256'].items())
  if not(r['normal_shutdown']and r['groups_empty']and r['inputs_unchanged']):rc=1
 except BaseException:r['cleanup_error']=traceback.format_exc();rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
