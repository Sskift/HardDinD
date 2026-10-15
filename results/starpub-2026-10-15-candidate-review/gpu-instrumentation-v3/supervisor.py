
from pathlib import Path
import datetime,fcntl,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;old=w/'gpu-mapping-owner-v14';repo=b/'repo';run=b/'run';name='gpu-instrument-v3';result=old/'run/results/mapping-cuda-v14';kernel=old/'kernel';disk=w/'runtime-shares-v1.ext4';base=['python3',str(repo/'scripts/harddind.py'),str(run),name]
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
import harddind
sha=harddind.sha
r={'status':'running','stage':'waiting_for_fp','commands':[],'cases':[],'gpu_async_wait':False,'comparison':'v2 same kernel/image/probe/runtime; async wait disabled','full_acceptance':False,'performance_acceptance':False,'sequence':['preload','none','both','strace','strace','both','none','preload'],'input_sha256':{str(p):sha(p)for p in [kernel,disk,result/'rootfs.tar',result/'cuda-probe',repo/'scripts/harddind.py',repo/'src/cuda_ioctl_trace.c',repo/'src/cuda_probe.c']}}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout=90,env=None):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 return row
def checked(label,cmd,timeout=90,env=None):
 row=call(label,cmd,timeout,env);assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],label;return row
def compute_records(label):
 records=[json.loads(x)for x in (b/(label+'.stdout')).read_text().splitlines() if x.startswith('{')]
 ok=len(records)==4 and {x['gpu']for x in records}==set(range(4)) and all(x['verified']is True and x['elements']==1048576 for x in records)
 return records,ok
save();rc=1
try:
 checked('socket-tests',['python3',str(repo/'tests/test_mconsole_socket.py')],30)
 deadline=time.monotonic()+1800
 while True:
  prior=json.loads((w/'fp-perf-owner-v1/owner-status.json').read_text())
  if prior['status']=='finished':break
  if time.monotonic()>deadline:raise TimeoutError('prior FP run')
  time.sleep(2)
 assert prior['owner_groups_empty'] and prior.get('performance_status',{}).get('groups_empty')
 os.sched_setaffinity(0,{0,1,2,3})
 for phase in ['native-before','docker','native-after']:
  r['stage']=phase;save()
  with (w/'kernel-build.lock').open('a')as lock:
   fcntl.flock(lock,fcntl.LOCK_SH if phase=='docker'else fcntl.LOCK_EX)
   if phase!='docker':
    cache=b/('cache-'+phase);cache.mkdir();env={**os.environ,'CUDA_CACHE_PATH':str(cache)};env.pop('LD_PRELOAD',None)
    checked(phase,[str(result/'cuda-probe')],90,env);records,ok=compute_records(phase);r[phase]={'compute':records,'all_four_verified':ok};assert ok;save();continue
   try:
    checked('start',base+['start','--kernel',str(kernel),'--disk',str(disk),'--gpu-control-experiment','--memfd-ram','--memfd-stub'],180);r['ready_state']=harddind.read_state(run/'instances'/name);save()
    checked('import',base+['docker','import',str(result/'rootfs.tar'),'harddind-gpu-instrument:local'],120)
    for index,mode in enumerate(r['sequence']):
     label=f'case-{index}-{mode}';container=f'gpu-instrument-{index}'
     devices=['--device=/dev/nvidiactl:/dev/nvidiactl']+[f'--device=/dev/nvidia{i}:/dev/nvidia{i}'for i in range(4)]+['--device=/dev/nvidia-uvm:/dev/nvidia-uvm']
     preload=['--env=LD_PRELOAD=/cuda-ioctl-trace.so']if mode in ['preload','both']else[]
     trace=['/usr/bin/strace','-f','-qq','-yy','-s','128','-o','/tmp/cuda-syscalls.strace','-e','trace=%file,ioctl,mmap,munmap,clone,clone3']if mode in ['strace','both']else[]
     row=call(label,base+['docker','run','--name',container,*devices,*preload,'--env=CUDA_CACHE_PATH=/tmp/cuda-cache','harddind-gpu-instrument:local',*trace,'/cuda-probe'],90)
     records,ok=compute_records(label);r['cases'].append({'index':index,'mode':mode,'exit_code':row['exit_code'],'timed_out':row.get('timeout',False),'compute':records,'all_four_verified':ok,'stderr_tail':(b/(label+'.stderr')).read_text()[-500:]});save()
     if trace:call('trace-'+str(index),base+['docker','cp',container+':/tmp/cuda-syscalls.strace',str(b/(label+'.strace'))],30)
     checked('remove-'+str(index),base+['docker','rm','-f',container],30)
   finally:
    checked('stop',base+['stop'],120);r['final_state']=harddind.read_state(run/'instances'/name);save()
 r['diagnostic_complete']=len(r['cases'])==8 and all(not x['timed_out']for x in r['cases']) and r['native-before']['all_four_verified']and r['native-after']['all_four_verified'];rc=0 if r['diagnostic_complete']else 1
except BaseException:r['error']=traceback.format_exc()
finally:
 try:
  st=harddind.read_state(run/'instances'/name)
  if harddind.live(st):call('final-stop',base+['stop'],120);st=harddind.read_state(run/'instances'/name)
  r['final_state']=st;r['normal_shutdown']=st.get('phase')=='stopped'and st.get('guest_exit_code')==0 and not st.get('forced_shutdown',True)
  gs={x['pid']for x in r['commands']if'pid'in x};gs.update(st.get(k)for k in ['guest_pid','network_pid','supervisor_pid']if st.get(k));r['groups']={str(g):group_members(g)for g in gs};r['groups_empty']=all(x==([],[])for x in r['groups'].values());r['inputs_unchanged']=all(sha(Path(p))==v for p,v in r['input_sha256'].items())
  if not(r['groups_empty']and r['normal_shutdown']and r['inputs_unchanged']):rc=1
 except BaseException:r['cleanup_error']=traceback.format_exc();rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
