import pathlib,json,os,subprocess,datetime,sys,signal,hashlib
b=pathlib.Path(__file__).resolve().parent;r=b/'repo';w=b/'build'
sys.path.insert(0,str(r/'scripts'));from guest_cleanup import cleanup_group
s={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'steps':[]}
def save():(b/'build-status.json').write_text(json.dumps(s,indent=2)+'\n')
def step(name,cmd,limit):
 row={'name':name,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()};s['steps'].append(row);save()
 with (b/(name+'.stdout')).open('xb') as out,(b/(name+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=r,env={**os.environ,'JOBS':'4','GOMAXPROCS':'4'},stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout=limit)
  except subprocess.TimeoutExpired:row['timed_out']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(timeout=15)
  row['cleanup']=cleanup_group(p.pid);row['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
 if row['exit_code'] or not row['cleanup']['clean_shutdown']:raise RuntimeError('failed '+name)
try:
 step('build-moby',['bash',str(r/'scripts/build-moby.sh'),str(w)],1800)
 step('build-network',['flock','-x',str(w/'kernel-build.lock'),'bash',str(r/'scripts/build-network.sh'),str(w)],180)
 step('build-port-agent',['flock','-x',str(w/'kernel-build.lock'),'bash',str(r/'scripts/build-port-agent.sh'),str(w)],300)
 step('make-runtime',['bash',str(r/'scripts/make-runtime-root.sh'),str(w),'/root/harddind-rebuild/protect-docker-root','runtime-publish-v3','--with-ports'],300)
 s['status']='finished';s['exit_code']=0
except BaseException as e:s['status']='finished';s['exit_code']=1;s['error']=repr(e)
s['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
