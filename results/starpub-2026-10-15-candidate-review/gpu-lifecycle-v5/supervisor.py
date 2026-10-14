from pathlib import Path
import datetime,fcntl,hashlib,importlib.util,json,os,signal,subprocess,sys,time,traceback
b=Path(__file__).resolve().parent;w=b.parent
sys.path.insert(0,str(b/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
r={'status':'running','stage':'waiting_for_prior_runtime','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False,'internal_gpu_acceptance':'not_tested','performance_acceptance':False,'commands':[],'source_sha256':{str(p.relative_to(b)):sha(p) for d in ['src','scripts','tests','docs'] for p in (b/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts}}
def save():(b/'owner-report.json').write_text(json.dumps(r,indent=2)+'\n')
def call(label,cmd,timeout=60,env=None,allow_failure=False):
 with (b/(label+'.stdout')).open('xb') as out,(b/(label+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=b,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);timed=False
  try:code=p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:timed=True;os.killpg(p.pid,signal.SIGKILL);code=p.wait(timeout=15)
  finally:cleanup=cleanup_group(p.pid)
 r['commands'].append({'label':label,'command':cmd,'pid':p.pid,'exit_code':code,'timed_out':timed,'cleanup':cleanup});save()
 if timed or not cleanup['clean_shutdown'] or (code and not allow_failure):raise RuntimeError('failed '+label)
 return (b/(label+'.stdout')).read_text()
save();rc=1
try:
 prior=w/'gpu-mapping-owner-v10'
 prior_state=json.loads((prior/'runtime-status.json').read_text());assert prior_state['status']=='finished'
 audit=json.loads((prior/'runtime-cleanup-audit.json').read_text());assert audit['all_empty']
 assert all(not live and not zombies for live,zombies in [group_members(int(g)) for g in audit['groups']])
 r['prior_runtime_status']=prior_state
 os.sched_setaffinity(0,{0,1,2,3})
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);r['stage']='host_validation';save()
  flags=['gcc','-O2','-Wall','-Wextra','-Werror','-std=gnu11','-pthread']
  call('build-probe',flags+['src/cuda_lifecycle_probe.c','-lm','-ldl','-o','build/cuda-lifecycle-probe'])
  for label,source,target in [('build-shared-json','tests/owner_shared_json.c','shared-json'),('build-protocol','tests/owner_protocol_regression.c','build/owner-protocol-regression'),('build-fixture','tests/cuda_lifecycle_host_fixture.c','build/cuda-lifecycle-host-fixture')]:
   extra=['-g','-fsanitize=address,undefined','-fno-sanitize-recover=all'] if label!='build-shared-json' else []
   call(label,flags+extra+[source,'-lm','-ldl','-o',target])
  fixed=call('shared-json',[str(b/'shared-json')]);rows=[json.loads(line) for line in fixed.splitlines()];assert [type(row['current_match']) for row in rows]==[bool,bool] and [row['current_match'] for row in rows]==[True,False]
  call('protocol-regression',[str(b/'build/owner-protocol-regression')])
  fixture=call('sanitizer-fixture',[str(b/'build/cuda-lifecycle-host-fixture')]);(b/'build/host-fixture.log').write_text(fixture)
  call('checker-tests',['python3','-m','unittest','discover','-s','tests','-p','test_gpu_lifecycle_checks.py','-v'])
  assert 'skipped=' not in (b/'checker-tests.stderr').read_text()
  r['binary_sha256']=sha(b/'build/cuda-lifecycle-probe');r['driver_version']=Path('/proc/driver/nvidia/version').read_text();r['library_sha256']={str(p):sha(p) for p in [Path('/lib/x86_64-linux-gnu/libcuda.so.1'),Path('/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1')]}
  query=['nvidia-smi','--query-gpu=uuid,name,memory.total,driver_version','--format=csv,noheader,nounits']
  before=call('inventory-before',query);uuids=[l.split(',')[0].strip() for l in before.splitlines() if l.strip()];assert len(uuids)==len(set(uuids))==4
  r['stage']='native_gpu_lifecycle';save()
  content=call('native',[str(b/'build/cuda-lifecycle-probe')],timeout=120,env={**os.environ,'CUDA_CACHE_PATH':str(b/'native-cache')},allow_failure=True)
  after=call('inventory-after',query);r['inventory_unchanged']=before==after
  spec=importlib.util.spec_from_file_location('checks',b/'scripts/gpu_lifecycle_checks.py');checks=importlib.util.module_from_spec(spec);spec.loader.exec_module(checks)
  verdict=checks.analyze(content,{'uuids':uuids});r['native_verdict']=verdict;(b/'native-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
  if not verdict['ok'] or not r['inventory_unchanged'] or next(c for c in r['commands'] if c['label']=='native')['exit_code']!=0:raise RuntimeError('native lifecycle failed')
  rc=0
except BaseException:r['error']=traceback.format_exc();rc=1
r['inputs_unchanged']=all(sha(b/n)==h for n,h in r['source_sha256'].items());r['libraries_unchanged']=all(sha(Path(n))==h for n,h in r.get('library_sha256',{}).items())
audit={str(c['pid']):group_members(c['pid']) for c in r['commands']};r['groups_empty']=all(not a and not z for a,z in audit.values());(b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
if not r['inputs_unchanged'] or not r['libraries_unchanged'] or not r['groups_empty']:rc=1
r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save();sys.exit(rc)
