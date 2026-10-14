from pathlib import Path
import collections,datetime,fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback,uuid
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';work=b/'work'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
CASES={'gp-syscall','fp-preserve','fp-signal-preserve','fp-signal-edit','gp-signal-edit','fp-nested','fp-threads-1','fp-threads-4','frame-churn','thread-exit-churn','runner-lifetime','fork-fp','exec-reset','ptrace-prefix-semantics','ptrace-gp-fp'}
r={'status':'running','stage':'preflight','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False,'performance_acceptance':False,'cells':{},'commands':[],'sources':{str(p.relative_to(repo)):sha(p) for p in repo.rglob('*') if p.is_file() and '__pycache__' not in p.parts},'limitations':['15-case diagnostic only; no throughput or performance acceptance','frame-churn wake FP oracle, child-inherited fork bytes and coredump notes remain untested','frozen lazy implementation is experimental and not integrated into main','native model results are not guest kernel evidence']}
def save():(b/'owner-status.json').write_text(json.dumps(r,indent=2)+'\n')
def acquire(mode):
 lock=(w/'kernel-build.lock').open('a');end=time.monotonic()+1800
 while True:
  try:fcntl.flock(lock,mode|fcntl.LOCK_NB);return lock
  except BlockingIOError:
   if time.monotonic()>end:lock.close();raise RuntimeError('lock deadline')
   time.sleep(1)
def call(label,cmd,timeout,env=None):
 with (b/(label+'.stdout')).open('xb') as o,(b/(label+'.stderr')).open('xb') as e:
  p=subprocess.Popen(cmd,cwd=repo,stdin=subprocess.DEVNULL,stdout=o,stderr=e,env=env,start_new_session=True);timed=False
  try:code=p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:timed=True;os.killpg(p.pid,signal.SIGKILL);code=p.wait(timeout=15)
  finally:cleanup=cleanup_group(p.pid)
 row={'label':label,'command':cmd,'pid':p.pid,'exit_code':code,'timed_out':timed,'cleanup':cleanup};r['commands'].append(row);save();return row

def classify(text,code,clean,timed,probe=None):
 rows=[];errors=[]
 for line in text.splitlines():
  if line.startswith('{'):
   try:row=json.loads(line)
   except ValueError:errors.append('malformed JSON');continue
   if row.get('test')!='fp_probe':errors.append('unknown JSON record');continue
   rows.append(row)
 seen=collections.Counter(x.get('case') for x in rows)
 if set(seen)!=CASES or any(n!=1 for n in seen.values()):errors.append('missing, duplicate or unknown cases')
 for x in rows:
  if x.get('status') not in ('pass','fail','skip') or type(x.get('ok')) is not bool or x['ok']!=(x['status']=='pass'):errors.append('invalid case status')
  if x.get('status')=='fail':errors.append('failed case '+str(x.get('case')))
 skipped=sorted(x.get('case') for x in rows if x.get('status')=='skip')
 if code!=0 or not clean or timed:errors.append('process exit, timeout or cleanup failure')
 if probe:
  hashes=[l.split('=',1)[1].strip() for l in text.splitlines() if l.startswith('HARDDIND_FP_PROBE_SHA256=')]
  ends=[l for l in text.splitlines() if l.startswith('HARDDIND_FP_END ')]
  codes=[l for l in text.splitlines() if l.startswith('HARDDIND_FP_PROBE_RC=')]
  if hashes!=[probe]:errors.append('probe hash mismatch or marker count')
  expected=2 if skipped else 0
  if ends!=['HARDDIND_FP_END status='+str(expected)] or codes!=['HARDDIND_FP_PROBE_RC='+str(expected)]:errors.append('completion marker mismatch')
 return {'verdict':'fail' if errors else 'incomplete' if skipped else 'pass','errors':errors,'skipped':skipped,'rows':rows}

save();exitcode=1
try:
 prior=json.loads((w/'posix-owner-v6/owner-status.json').read_text());assert prior['status']=='finished' and prior['exit_code']==0
 audit=json.loads((w/'posix-owner-v6/owner-cleanup-audit.json').read_text());assert audit['all_empty'] and all(group_members(int(g))==([],[]) for g in audit['groups'])
 os.sched_setaffinity(0,{0,1,2,3});r['stage']='build_and_native_probe';save()
 lock=acquire(fcntl.LOCK_EX)
 try:
  row=call('strict-build',['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread',str(repo/'src/fp_probe.c'),'-o',str(b/'native-probe')],60);assert row['exit_code']==0 and row['cleanup']['clean_shutdown']
  row=call('build-disk',['bash',str(repo/'scripts/fp-make-root.sh'),str(work)],180);assert row['exit_code']==0 and row['cleanup']['clean_shutdown']
  r['probe_sha256']=sha(work/'fp-root/fp-probe');assert r['probe_sha256']==sha(b/'native-probe')
  r['disk_sha256']=sha(work/'fp.ext4');r['kernels']={n:sha(b/(n+'-kernel')) for n in ['eager','lazy']}
  row=call('native',[str(b/'native-probe')],90);content=(b/'native.stdout').read_text();native=classify(content,row['exit_code'],row['cleanup']['clean_shutdown'],row['timed_out']);r['native']=native;save();assert native['verdict']=='pass'
  assert classify(content+content.splitlines()[0]+'\n',0,True,False)['verdict']=='fail'
  assert classify('\n'.join(content.splitlines()[1:]),0,True,False)['verdict']=='fail'
  assert classify(content+'{broken\n',0,True,False)['verdict']=='fail'
  assert classify(content,0,False,False)['verdict']=='fail'
  assert classify(content,0,True,True)['verdict']=='fail'
  assert classify(content,0,True,False,'wrong')['verdict']=='fail'
  r['classifier_negative_checks']=6;save()
 finally:lock.close()
 (work/'tmp').mkdir(exist_ok=True)
 cells=[(k,c,p,l) for k in ['eager','lazy'] for c in [1,4] for p in [False,True] for l in ([False] if k=='eager' else [False,True])]
 for kernel,cpus,parallel,lazy in cells:
  name=f'{kernel}-{cpus}cpu-p{int(parallel)}-l{int(lazy)}';r['stage']='guest '+name;save();lock=acquire(fcntl.LOCK_SH);cow=work/'tmp'/('fp-'+uuid.uuid4().hex+'.cow')
  try:
   cmd=[str(b/(kernel+'-kernel')),'umid=harddind-'+uuid.uuid4().hex[:12],'mem=2G',f'ncpus={cpus}','seccomp=on',f'ubd0={cow},{work}/fp.ext4','root=/dev/ubda','rw','init=/sbin/harddind-fp-init','con=null','con0=null,fd:1','ssl=null','panic=1']
   if parallel:cmd.append('seccomp_threads=1')
   if lazy:cmd.append('lazyxstate=1')
   row=call(name,cmd,180,env={**os.environ,'TMPDIR':str(work/'tmp')});text=(b/(name+'.stdout')).read_text(errors='replace')+'\n'+(b/(name+'.stderr')).read_text(errors='replace')
   verdict=classify(text,row['exit_code'],row['cleanup']['clean_shutdown'],row['timed_out'],r['probe_sha256']);unchanged=sha(b/(kernel+'-kernel'))==r['kernels'][kernel]
   if not unchanged:verdict['verdict']='fail';verdict['errors'].append('kernel changed')
   verdict.update(kernel=kernel,cpus=cpus,parallel=parallel,lazy=lazy,kernel_unchanged=unchanged,command=row)
   r['cells'][name]=verdict;save()
   if group_members(row['pid'])!=([],[]):raise RuntimeError('residual guest group; stop matrix')
  finally:lock.close();cow.unlink(missing_ok=True)
 r['inputs_unchanged']=all(sha(repo/n)==h for n,h in r['sources'].items()) and sha(work/'fp.ext4')==r['disk_sha256']
 r['all_cells_passed']=len(r['cells'])==12 and all(x['verdict']=='pass' for x in r['cells'].values())
 exitcode=0 if r['inputs_unchanged'] and r['all_cells_passed'] else 1
except BaseException:r['error']=traceback.format_exc()
audit={str(c['pid']):group_members(c['pid']) for c in r['commands']};r['groups_empty']=all(v==([],[]) for v in audit.values());(b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
if not r['groups_empty']:exitcode=1
r.update(status='finished',exit_code=exitcode,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save();sys.exit(exitcode)
