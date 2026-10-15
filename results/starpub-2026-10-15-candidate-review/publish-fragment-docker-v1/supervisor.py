from pathlib import Path
import datetime,hashlib,json,os,signal,subprocess,sys,time,traceback,shutil
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';run=b/'run';sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
import harddind
r={'status':'running','stage':'wait_native_v2','commands':[],'full_acceptance':False,'performance_acceptance':False}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout):
 row={'label':label,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb')as out,(b/(label+'.stderr')).open('xb')as err:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timeout']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(15)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 assert row['exit_code']==0 and not row.get('timeout') and row['cleanup']['clean_shutdown'],label
save();rc=1
try:
 deadline=time.monotonic()+900
 while True:
  p=w/'publish-fragment-native-owner-v2/owner-status.json';j=json.loads(p.read_text())
  if j['status']=='finished':break
  if time.monotonic()>deadline:raise TimeoutError('native queue900s')
  time.sleep(2)
 assert j['exit_code']==0 and j['groups_empty'] and j['inputs_unchanged'];r['native_gate_sha256']=harddind.sha(p)
 shutil.copy2(w/'publish-fragment-native-owner-v2/build/network-backend',run/'network-backend')
 assert harddind.sha(run/'network-backend')==j['backend_sha256']['release']
 r['source_before']={str(p.relative_to(repo)):harddind.sha(p)for p in repo.rglob('*')if p.is_file()and '__pycache__'not in p.parts}
 r['stage']='nested_docker_full_publish_subset';save()
 call('docker-publish',['python3',str(repo/'scripts/run-published-ports.py'),str(run),'--name','pubfrag1','--kernel',str(w/'cpu-cpuset-kernel-v1'),'--disk',str(w/'publish-owner-v3/build/runtime-publish-v3.ext4')],2400)
 j=json.loads((run/'results/pubfrag1.json').read_text());r['checks']=j['checks'];r['subset_passed']=j['subset_passed'];r['echo_count']=len(j['echoes']);assert j['subset_passed']
 rc=0
except BaseException:r['error']=traceback.format_exc()
finally:
 try:
  state=harddind.read_state(run/'instances/pubfrag1')
  if harddind.live(state):call('final-stop',['python3',str(repo/'scripts/harddind.py'),str(run),'pubfrag1','stop'],90);state=harddind.read_state(run/'instances/pubfrag1')
  r['final_state']=state;r['normal_shutdown']=state.get('phase')=='stopped'and state.get('guest_exit_code')==0 and not state.get('forced_shutdown',True)
  groups={x['pid']for x in r['commands']if'pid'in x};groups.update(state[k]for k in ['guest_pid','network_pid','supervisor_pid']if state.get(k))
  report=run/'results/pubfrag1.json'
  if report.exists():
   j=json.loads(report.read_text());r['checks']=j['checks'];r['subset_passed']=j['subset_passed'];r['echo_count']=len(j['echoes']);groups.update(x['cleanup']['pgid']for x in j['commands']);r['inputs_unchanged']=j['inputs_unchanged']
  r['groups']={str(g):group_members(g)for g in sorted(groups)};r['groups_empty']=all(v==([],[])for v in r['groups'].values())
  r['source_after']={str(p.relative_to(repo)):harddind.sha(p)for p in repo.rglob('*')if p.is_file()and '__pycache__'not in p.parts};r['source_unchanged']=r.get('source_before')==r['source_after']
  if not(r['normal_shutdown']and r['groups_empty']and r.get('inputs_unchanged')and r['source_unchanged']):rc=1
 except BaseException:r['cleanup_error']=traceback.format_exc();rc=1
 r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
