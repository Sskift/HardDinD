from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent
sys.dont_write_bytecode=True
r={'status':'running','stage':'await-owner-clock-limits','commands':[],'full_acceptance':False,'performance_acceptance':False,'guest_verification':'not_tested'}
def save():(b/'status.json').write_text(json.dumps(r,indent=2)+'\n')
def call(name,args,timeout):
 row={'name':name,'command':args};r['commands'].append(row);save()
 with (b/(name+'.stdout')).open('xb') as so,(b/(name+'.stderr')).open('xb') as se:
  p=subprocess.Popen(args,cwd=b,stdin=subprocess.DEVNULL,stdout=so,stderr=se,start_new_session=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
  row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:
   row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(10)
  finally:
   row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],row
save();rc=1
try:
 sys.path.insert(0,str(w/'cpu-clock-limits-owner-v1/repo/scripts'));from guest_cleanup import cleanup_group,group_members
 deadline=time.monotonic()+1200
 while True:
  state=json.loads((w/'cpu-clock-limits-owner-v1/owner-status.json').read_text())
  if state['status']=='finished':
   assert state['groups_empty'],state
   break
  assert Path('/proc/2694566').is_dir(),'owner missing with nonterminal state; inspect before proceeding'
  assert time.monotonic()<deadline,'owner wait exceeded1200s'
  time.sleep(1)
 os.sched_setaffinity(0,{0,1,2,3})
 r['inputs']={str(p.relative_to(b)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ['src','scripts','tests'] for p in (b/folder).rglob('*') if p.is_file()}
 with (w/'kernel-build.lock').open('a') as lock:
  deadline=time.monotonic()+180
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>deadline:raise TimeoutError('EX lock180s')
    time.sleep(.1)
  r['stage']='bounded-host-validation';save();started=time.monotonic()
  call('compile',['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread','src/cpu_limit_probe.c','-o','cpu-limit-probe'],30)
  call('selftest',[str(b/'cpu-limit-probe'),'selftest'],5)
  call('unit-tests',['python3','-m','unittest','discover','-s','tests','-p','test_cpu_limit_checks.py'],20)
  call('native-spin',[str(b/'cpu-limit-probe'),'spin','--threads','4','--duration-ms','1000','--sample-ms','100'],10)
  r['locked_wall_seconds']=time.monotonic()-started
  assert r['locked_wall_seconds']<=60
 sys.path.insert(0,str(b/'scripts'))
 import cpu_limit_checks as checks
 events=checks.parse_events((b/'native-spin.stdout').read_text())
 summary=checks.spin_summary(events);r['completed_work']=summary['completed_work'];assert r['completed_work']['status']=='pass' and r['completed_work']['total_blocks']>0
 r['input_unchanged']=all(hashlib.sha256((b/n).read_bytes()).hexdigest()==h for n,h in r['inputs'].items())
 r['binary_sha256']=hashlib.sha256((b/'cpu-limit-probe').read_bytes()).hexdigest()
 r['groups']={str(x['pid']):group_members(x['pid']) for x in r['commands']}
 assert all(v==([],[]) for v in r['groups'].values()) and r['input_unchanged']
 rc=0
except BaseException:r['error']=traceback.format_exc()
finally:r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
sys.exit(rc)
