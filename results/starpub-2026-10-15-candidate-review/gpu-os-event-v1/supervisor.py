import pathlib,os,subprocess,signal,time,json,fcntl,hashlib,traceback
b=pathlib.Path(__file__).resolve().parent;w=b.parent
report={'status':'running','commands':[],'groups':[],'native':{},'limitations':['Only native OS-event ABI; no internal CUDA or notification trigger.','Frozen v1 probe may have instrumentation defects; raw outputs retained.']}
def save():
 p=b/'owner-report.tmp';p.write_text(json.dumps(report,indent=2)+'\n');p.replace(b/'owner-report.json')
def members(pgid):
 found=[]
 for p in pathlib.Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   s=(p/'stat').read_text().rsplit(')',1)[1].split()
   if int(s[2])==pgid:found.append({'pid':int(p.name),'state':s[0]})
  except (OSError,ValueError):pass
 return found
def run(name,cmd,timeout=60,env=None):
 row={'name':name,'argv':cmd,'timeout':timeout,'started':time.time()};report['commands'].append(row);save()
 with (b/(name+'.stdout')).open('wb') as out,(b/(name+'.stderr')).open('wb') as err:
  p=subprocess.Popen(cmd,cwd=b,stdout=out,stderr=err,start_new_session=True,env=env);row['pid']=row['pgid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:
   row['timeout_expired']=True
   os.killpg(p.pid,signal.SIGTERM)
   try:p.wait(3)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(3)
   row['exit_code']=p.returncode
  left=members(p.pid);row['remaining_before_cleanup']=left
  if left:
   try:os.killpg(p.pid,signal.SIGKILL)
   except ProcessLookupError:pass
   time.sleep(.1)
  row['remaining_after_cleanup']=members(p.pid);row['elapsed']=time.time()-row['started'];save()
 return row
os.sched_setaffinity(0,{0,1,2,3});save()
with (w/'kernel-build.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 try:
  (b/'build').mkdir()
  r=run('build',['gcc','-O2','-static','-std=gnu11','-Wall','-Wextra','-Werror',str(b/'src/gpu_os_event_probe.c'),'-o',str(b/'build/gpu-os-event-probe')])
  if r['exit_code']:raise RuntimeError('compile failed')
  report['binary_sha256']=hashlib.sha256((b/'build/gpu-os-event-probe').read_bytes()).hexdigest();save()
  run('layout',[str(b/'build/gpu-os-event-probe'),'--layout-selfcheck'])
  run('layout-check',['python3',str(b/'scripts/gpu_os_event_checks.py'),str(b/'layout.stdout'),'--layout'])
  run('inventory-before',['nvidia-smi','--query-gpu=index,uuid,driver_version','--format=csv,noheader'])
  for i in range(4):
   name='native-'+str(i);r=run(name,[str(b/'build/gpu-os-event-probe'),'--device','/dev/nvidia'+str(i),'--poll-ms','30'],30)
   c=run(name+'-check',['python3',str(b/'scripts/gpu_os_event_checks.py'),str(b/(name+'.stdout'))],30)
   report['native'][str(i)]={'probe_exit_code':r['exit_code'],'checker_exit_code':c['exit_code']};save()
  run('inventory-after',['nvidia-smi','--query-gpu=index,uuid,driver_version','--format=csv,noheader'])
  report['status']='finished'
 except Exception as e:report['status']='failed';report['error']=str(e);report['traceback']=traceback.format_exc()
 finally:
  manifest=json.loads((b/'source-manifest.json').read_text());report['sources_unchanged']=all(hashlib.sha256((b/p).read_bytes()).hexdigest()==v for p,v in manifest.items());report['cleanup_ok']=all(not r.get('remaining_after_cleanup') for r in report['commands']);report['finished']=time.time();save()
