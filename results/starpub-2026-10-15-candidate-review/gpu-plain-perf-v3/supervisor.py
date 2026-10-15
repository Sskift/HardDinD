
from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,statistics,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';run=b/'run';name='gpu-plain-v3'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
import harddind
sha=harddind.sha;inputs=json.loads((b/'input-cuda-report.json').read_text());kernel=w/'gpu-completion-owner-v1/kernel';disk=w/'runtime-shares-v1.ext4';base=['python3',str(repo/'scripts/harddind.py'),str(run),name]
r={'status':'running','stage':'init','commands':[],'samples':{},'full_acceptance':False,'performance_acceptance':False,'descriptive_only':True,'warmups':2,'timed':5,'instrumentation':'no LD_PRELOAD, no strace','input_sha256':{str(p):sha(p) for p in [kernel,disk,b/'cuda-probe',b/'rootfs.tar',repo/'scripts/harddind.py',repo/'src/cuda_probe.c',w/'network-backend',w/'client/docker',w/'client/ld-musl-x86_64.so.1']},'library_sha256':inputs['library_sha256'],'host_affinity':[0,1,2,3],'limitations':['per-GPU measured region is two HtoD copies + vector-add + sync + DtoH; allocation/context creation excluded','fresh process per repetition; Docker container startup is recorded separately and is not compared to native Docker','sequential cards only; not a whole GPU performance acceptance']}
t0=time.monotonic()
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout=90,env=None):
 if time.monotonic()-t0>900:raise TimeoutError('global run budget')
 row={'label':label,'command':cmd,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save();start=time.monotonic()
 with (b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['wall_seconds']=time.monotonic()-start;row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],label
 return row
save();rc=1;os.sched_setaffinity(0,{0,1,2,3})
try:
 assert all(sha(Path(p))==h for p,h in r['library_sha256'].items())
 prior=json.loads((w/'gpu-completion-owner-v1/owner-status.json').read_text());assert prior['status']=='finished' and prior['groups_empty'] and prior['exit_code']==0
 for phase in ['native-before','docker','native-after']:
  r['stage']=phase;save()
  with (w/'kernel-build.lock').open('a')as lock:
   lock_start=time.monotonic()
   while True:
    try:fcntl.flock(lock,(fcntl.LOCK_SH if phase=='docker' else fcntl.LOCK_EX)|fcntl.LOCK_NB);break
    except BlockingIOError:
     if time.monotonic()-lock_start>180:raise TimeoutError('kernel-build lock 180s wait')
     time.sleep(.25)
   r.setdefault('lock_wait_seconds',{})[phase]=time.monotonic()-lock_start;save()
   try:
    if phase=='docker':
     call('start',base+['start','--kernel',str(kernel),'--disk',str(disk),'--gpu-control-experiment','--gpu-async-wait','--memfd-ram','--memfd-stub'],180)
     r['ready_state']=harddind.read_state(run/'instances'/name);save()
     call('import',base+['docker','import',str(b/'rootfs.tar'),'harddind-gpu-plain:local'],120)
    r['samples'][phase]=[]
    for seq in range(7):
     label=phase+'-'+str(seq);cache=b/('cache-'+label);cache.mkdir();env={**os.environ,'CUDA_CACHE_PATH':str(cache)};env.pop('LD_PRELOAD',None)
     devices=['--device=/dev/nvidiactl:/dev/nvidiactl']+[f'--device=/dev/nvidia{i}:/dev/nvidia{i}'for i in range(4)]+['--device=/dev/nvidia-uvm:/dev/nvidia-uvm']
     cmd=[str(b/'cuda-probe')] if phase!='docker' else base+['docker','run','--rm','--name','gpu-plain-'+str(seq),*devices,'--env=CUDA_CACHE_PATH=/tmp/cuda-cache','harddind-gpu-plain:local','/cuda-probe']
     row=call(label,cmd,90,env);records=[json.loads(x)for x in (b/(label+'.stdout')).read_text().splitlines()]
     assert len(records)==4 and {x['gpu']for x in records}==set(range(4)) and all(x['verified'] is True and x['elements']==1048576 and x['seconds']>0 for x in records)
     assert not(b/(label+'.stderr')).read_bytes(),label+' unexpected stderr'
     r['samples'][phase].append({'seq':seq,'warmup':seq<2,'gpu_results':records,'command_wall_seconds':row['wall_seconds']});save()
   finally:
    if phase=='docker':
     call('stop',base+['stop'],120);r['final_state']=harddind.read_state(run/'instances'/name);save()
 summary={}
 for phase,samples in r['samples'].items():
  summary[phase]={}
  for gpu in range(4):
   vals=[next(x['seconds']for x in sample['gpu_results']if x['gpu']==gpu)for sample in samples if not sample['warmup']];median=statistics.median(vals)
   summary[phase][str(gpu)]={'median_seconds':median,'mad_seconds':statistics.median(abs(x-median)for x in vals),'raw_seconds':vals}
 r['summary']=summary;r['ratios']={phase:{str(gpu):summary['docker'][str(gpu)]['median_seconds']/summary[phase][str(gpu)]['median_seconds']for gpu in range(4)}for phase in ['native-before','native-after']}
 rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 try:
  st=harddind.read_state(run/'instances'/name)
  if harddind.live(st):call('final-stop',base+['stop'],120);st=harddind.read_state(run/'instances'/name)
  r['final_state']=st;r['normal_shutdown']=st.get('phase')=='stopped' and st.get('guest_exit_code')==0 and not st.get('forced_shutdown',True)
  groups={x['pid']for x in r['commands']if'pid'in x}
  groups.update(st.get(k)for k in ['guest_pid','network_pid','supervisor_pid']if st.get(k))
  r['groups']={str(g):group_members(g)for g in sorted(groups)};r['groups_empty']=all(x==([],[])for x in r['groups'].values())
  r['inputs_unchanged']=all(sha(Path(p))==h for p,h in r['input_sha256'].items())and all(sha(Path(p))==h for p,h in r['library_sha256'].items())
  if not(r['normal_shutdown']and r['groups_empty']and r['inputs_unchanged']):rc=1
 except BaseException:r['cleanup_error']=traceback.format_exc();rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
