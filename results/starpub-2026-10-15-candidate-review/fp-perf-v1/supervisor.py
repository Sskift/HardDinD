
from pathlib import Path
import datetime,fcntl,hashlib,json,os,signal,shutil,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent;a=w/'arkclaude/round13/fp-v4-review';h=b/'harness';work=b/'run'
sys.path.insert(0,str(w/'fp-matrix-owner-v1/repo/scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_gpu','commands':[],'full_acceptance':False,'performance_acceptance':False}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout,cwd=None):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,cwd=cwd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:
   row['timeout']=True;os.killpg(p.pid,signal.SIGINT)
   try:row['exit_code']=p.wait(120)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 return row
save();rc=1
try:
 deadline=time.monotonic()+1200
 while True:
  prior=json.loads((w/'gpu-plain-perf-owner-v1/owner-status.json').read_text())
  if prior['status']=='finished':break
  if time.monotonic()>deadline:raise TimeoutError('prior GPU experiment')
  time.sleep(2)
 assert prior['groups_empty'] and prior['normal_shutdown'] and prior['inputs_unchanged']
 assert all(group_members(int(g))==([],[])for g in prior['groups'])
 assert json.loads((a.parent/'fp-v4-review.resume6.status.json').read_text())['status']=='finished'
 with (w/'kernel-build.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  r['stage']='freeze_and_preflight';save();h.mkdir();work.mkdir()
  for p in (a/'perf').iterdir():
   if p.is_file():shutil.copy2(p,h/p.name)
  shutil.copy2(a/'REPORT-PERF-HARNESS-v2.md',b/'author-report.md')
  shutil.copytree(a/'perf/work/bin',work/'bin');shutil.copy2(a/'perf/work/perf.ext4',work/'perf.ext4');shutil.copy2(a/'perf/work/perf-manifest.json',work/'perf-manifest.json')
  r['harness_before']={str(p.relative_to(h)):sha(p)for p in h.rglob('*')if p.is_file() and '__pycache__'not in p.parts};save()
  row=call('harness-tests',['python3','-m','unittest','test_perf_log','test_supervisor_paths'],180,cwd=h);assert row['exit_code']==0 and row['cleanup']['clean_shutdown']
 r['stage']='actual_performance';save()
 row=call('performance',['python3',str(h/'fp-perf-supervisor.py'),'--work',str(work),'--no-prep','--budget','1200'],1500,cwd=h);rc=row['exit_code']
except BaseException:r['error']=traceback.format_exc();rc=1
finally:
 if h.exists():
  r['harness_after']={n:sha(h/n)for n in r.get('harness_before',{})};r['harness_unchanged']=r.get('harness_before')==r['harness_after']
 p=work/'perf-status.json'
 if p.exists():
  d=json.loads(p.read_text());r['performance_status']={k:d.get(k)for k in ['status','exit_code','groups_empty','inputs_unchanged','error']};r['cells']={k:{f:v.get(f)for f in ['exit_code','clean_shutdown','errors','timed_out','status']}for k,v in d.get('cells',{}).items()}
 r['owner_groups']={str(c['pid']):group_members(c['pid'])for c in r['commands']if'pid'in c};r['owner_groups_empty']=all(x==([],[])for x in r['owner_groups'].values())
 if not r.get('harness_unchanged') or not r['owner_groups_empty']:rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
