import pathlib,json,os,subprocess,datetime,sys,signal,hashlib
b=pathlib.Path(__file__).resolve().parent;r=b/'repo';w=b/'build'
sys.path.insert(0,str(r/'scripts'));from guest_cleanup import cleanup_group
s={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'steps':[]}
def save():(b/'run-status.json').write_text(json.dumps(s,indent=2)+'\n')
def step(name,cmd,limit,env=None,cwd=None,expected=0):
 row={'name':name,'command':cmd,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'expected':expected};s['steps'].append(row);save()
 with (b/'validation'/(name+'.stdout')).open('xb') as out,(b/'validation'/(name+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=cwd or r,env=env or os.environ,stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout=limit)
  except subprocess.TimeoutExpired:row['timed_out']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(timeout=15)
  row['cleanup']=cleanup_group(p.pid);row['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
 if row['exit_code']!=expected or row.get('timed_out') or not row['cleanup']['clean_shutdown']:raise RuntimeError('failed '+name)
try:
 lock=['flock','-x',str(w/'kernel-build.lock')]
 step('python-syntax',lock+['python3','-m','py_compile',str(r/'scripts/run-published-ports.py'),str(r/'scripts/port_driver.py')],90)
 step('network-addresses',lock+[str(w/'network-probe'),'addresses','lo'],90)
 addresses=(b/'validation/network-addresses.stdout').read_text();assert '4 127.0.0.1\n' in addresses and '6 ::1\n' in addresses
 step('moby-script-syntax',lock+['bash','-n',str(r/'scripts/build-moby.sh')],90)
 step('moby-jobs-invalid',lock+['bash',str(r/'scripts/build-moby.sh'),str(w)],90,env={**os.environ,'JOBS':'bad'},expected=1)
 assert 'JOBS must be a positive integer' in (b/'validation/moby-jobs-invalid.stderr').read_text()
 step('port-driver-tests',lock+['python3','-m','unittest','discover','-s','tests','-p','test_port_driver.py'],90)
 step('native-backend-tests',lock+['python3','-m','unittest','discover','-s','tests/publish','-p','test_*.py'],120,env={**os.environ,'HARDDIND_NETWORK_BACKEND':str(w/'network-backend')})
 src=next(p for p in w.glob('moby-port-*') if p.is_dir() and p.name not in ('moby-port-bin','moby-port-pristine'))
 env={**os.environ,'GOMAXPROCS':'4','GOFLAGS':'-mod=vendor -p=4','GOPROXY':'off','GOSUMDB':'off','GOTOOLCHAIN':'local','CGO_ENABLED':'0','GOCACHE':str(w/'cache/go-build')}
 step('moby-alias-tests',lock+[str(w/'go-build-toolchain/bin/go'),'test','-timeout','90s','./daemon/libnetwork/internal/rlkclient','./daemon/libnetwork/portmappers/nat'],180,env=env,cwd=src)
 step('docker-runtime',['python3',str(r/'scripts/run-published-ports.py'),str(w),'--name','publish-v3','--kernel','/root/harddind-rebuild/cpu-cpuset-kernel-v1','--disk',str(w/'runtime-publish-v3.ext4')],1800)
 s['status']='finished';s['exit_code']=0
except BaseException as e:s['status']='finished';s['exit_code']=1;s['error']=repr(e)
s['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
