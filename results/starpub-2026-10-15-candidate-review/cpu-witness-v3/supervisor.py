from pathlib import Path
import collections,datetime,fcntl,hashlib,importlib.util,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members

def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for c in iter(lambda:f.read(1048576),b''):h.update(c)
 return h.hexdigest()
def save():(b/'owner-status.json').write_text(json.dumps(r,indent=2)+'\n')
def acquire():
 f=(w/'kernel-build.lock').open('a');end=time.monotonic()+1800
 while True:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);return f
  except BlockingIOError:
   if time.monotonic()>end:raise RuntimeError('EX lock deadline')
   time.sleep(1)
def run(label,cmd,timeout):
 with (b/(label+'.stdout')).open('xb') as o,(b/(label+'.stderr')).open('xb') as e:
  p=subprocess.Popen(cmd,cwd=b,stdin=subprocess.DEVNULL,stdout=o,stderr=e,start_new_session=True);r['running_command']={'label':label,'pid':p.pid};save();timed=False
  try:rc=p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:timed=True;os.killpg(p.pid,signal.SIGKILL);rc=p.wait(timeout=15)
  finally:cl=cleanup_group(p.pid)
 row={'label':label,'command':cmd,'pid':p.pid,'exit_code':rc,'timed_out':timed,'cleanup':cl};r['commands'].append(row);save();return row
r={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False,'performance_acceptance':False,'stage':'preflight','commands':[],'sources_before':{str(p.relative_to(b)):sha(p) for d in ['diag','repo'] for p in (b/d).rglob('*') if p.is_file() and not p.name.startswith('._') and '__pycache__' not in p.parts}};save();rc=1
try:
 os.sched_setaffinity(0,{0,1,2,3})
 prior=json.loads((w/'gpu-mapping-owner-v11/runtime-status.json').read_text());assert prior['status']=='finished' and prior['groups_empty'] and prior['inputs_unchanged']
 audit=json.loads((w/'gpu-mapping-owner-v11/runtime-cleanup-audit.json').read_text());assert audit['all_empty'] and all(group_members(int(g))==([],[]) for g in audit['groups'])
 m=json.loads((b/'diag/MANIFEST-v3.json').read_text())
 for n,h in m['files'].items():assert sha(b/'diag'/Path(n).relative_to('diag/v3'))==h,n
 for n in ['busy_probe','kernel','disk']:assert sha(Path(m['frozen_inputs'][n]))==m['frozen_inputs'][n+'_sha256'],n
 r['manifest_verified']=True;save()
 lock=acquire()
 try:
  row=run('owner-fixtures',['python3',str(b/'fixture-runner.py')],120);assert row['exit_code']==0 and row['cleanup']['clean_shutdown']
 finally:lock.close()
 r['stage']='guest_witness';save()
 row=run('runtime',['python3',str(b/'diag/run-cpu-witness.py'),str(b/'run'),'witness-v3','--kernel',m['frozen_inputs']['kernel'],'--disk',m['frozen_inputs']['disk'],'--repo',str(repo),'--count','60','--interval-ms','100','--affinity','0,1,2,3','--cpus','4'],1200)
 report=json.loads((b/'run/results/witness-v3/report.json').read_text());r['runtime_exit_code']=row['exit_code'];r['runtime_normal_shutdown']=report.get('normal_shutdown');r['runtime_inputs_unchanged']=report.get('inputs_unchanged');r['cells']={n:v['status'] for n,v in report['cells'].items()}
 rc=0 if row['exit_code']==0 and row['cleanup']['clean_shutdown'] and report['normal_shutdown'] and report['inputs_unchanged'] and set(r['cells'])=={'quiet-quota','quiet-unlimited','separate-observer','same-observer'} and set(r['cells'].values())=={'complete'} else 1
except BaseException:r['error']=traceback.format_exc()
def groups(x):
 out=set()
 if isinstance(x,dict):
  if 'process_group' in x and 'clean_shutdown' in x:out.add(x['process_group'])
  for v in x.values():out.update(groups(v))
 elif isinstance(x,list):
  for v in x:out.update(groups(v))
 return out
allgroups=groups(r)
for p in (b/'run').glob('results/*/report.json'):allgroups.update(groups(json.loads(p.read_text())))
for p in (b/'run').glob('instances/*/state.json'):allgroups.update(groups(json.loads(p.read_text())))
audit={str(g):group_members(g) for g in sorted(allgroups)};(b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n');r['groups_empty']=all(x==([],[]) for x in audit.values());r['inputs_unchanged']=all(sha(b/n)==h for n,h in r['sources_before'].items())
if not r['groups_empty'] or not r['inputs_unchanged']:rc=1
r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save();sys.exit(rc)
