
from pathlib import Path
import json,hashlib,subprocess,os,signal,fcntl,sys,datetime,traceback,time,re
b=Path(__file__).resolve().parent;w=b.parent;sys.path.insert(0,str(w/'gpu-mapping-owner-v13/repo/scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','commands':[],'full_acceptance':False,'internal_cuda_verified':False,'performance_acceptance':False,'sources':{n:sha(b/n)for n in ['cuda_probe.c','cuda_ioctl_trace.c','original-cuda-ioctl-trace.c','cuda-probe']},'library_sha256':json.loads((b/'library-sha256.json').read_text())}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,env=None,timeout=90):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],label
save();rc=1
try:
 prior=json.loads((w/'cpu-shares-owner-v6/owner-status.json').read_text());assert prior['status']=='finished' and prior['groups_empty'] and prior['normal_shutdown']
 assert all(sha(Path(p))==h for p,h in r['library_sha256'].items())
 os.sched_setaffinity(0,{0,1,2,3})
 with (w/'kernel-build.lock').open('a')as lock:
  end=time.monotonic()+1200
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>end:raise TimeoutError('global lock')
    time.sleep(1)
  call('build-logger',['gcc','-O2','-Wall','-Wextra','-Werror','-shared','-fPIC',str(b/'cuda_ioctl_trace.c'),'-o',str(b/'cuda-ioctl-trace.so')])
  r['logger_sha256']=sha(b/'cuda-ioctl-trace.so');save()
  call('native-cuda',[str(b/'cuda-probe')],env={**os.environ,'LD_PRELOAD':str(b/'cuda-ioctl-trace.so')})
 r['compute']=[json.loads(x)for x in (b/'native-cuda.stdout').read_text().splitlines()];assert len(r['compute'])==4 and all(x['verified']for x in r['compute'])
 selected=[]
 for line in (b/'native-cuda.stderr').read_text().splitlines():
  row=dict(re.findall(r'(\w+)=([^ ]*)',line))
  if row.get('rm_command') in ['0x20808159','0x20810108','0x20808162']:
   assert len(bytes.fromhex(row['params_before']))==len(bytes.fromhex(row['params_after']))==int(row['params_size'])
   assert row['rc']=='0' and int.from_bytes(bytes.fromhex(row['after'])[28:32],'little')==0
   selected.append(row)
 r['full_captures']=selected;r['counts']={cmd:sum(x['rm_command']==cmd for x in selected)for cmd in ['0x20808159','0x20810108','0x20808162']};assert all(n==4 for n in r['counts'].values());rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 r['sources_unchanged']=all(sha(b/n)==h for n,h in r['sources'].items());r['libraries_unchanged']=all(sha(Path(p))==h for p,h in r['library_sha256'].items());r['groups']={str(x['pid']):group_members(x['pid'])for x in r['commands']if 'pid'in x};r['groups_empty']=all(x==([],[])for x in r['groups'].values())
 if not (r['sources_unchanged'] and r['libraries_unchanged'] and r['groups_empty']):rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
