import pathlib,json,os,subprocess,datetime,sys,signal,hashlib,fcntl,socket,time
b=pathlib.Path(__file__).resolve().parent;w=b/'run';repo=pathlib.Path('/root/harddind-rebuild/publish-owner-v3/repo');sys.path.insert(0,str(repo/'scripts'))
import harddind
from guest_cleanup import cleanup_group
instance=w/'instances/publish-diag2';base=['python3',str(repo/'scripts/harddind.py'),str(w),'publish-diag2']
r={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False,'performance_acceptance':False,'diagnostic_complete':False,'commands':[],'echoes':[]}
def save():(b/'report.json').write_text(json.dumps(r,indent=2)+'\n')
def run(label,args,timeout=60,require=True):
 p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,start_new_session=True);row={'label':label,'args':args,'pid':p.pid};r['commands'].append(row);save()
 try:out,err=p.communicate(timeout=timeout)
 except subprocess.TimeoutExpired:row['timed_out']=True;os.killpg(p.pid,signal.SIGKILL);out,err=p.communicate(timeout=15)
 finally:
  if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=15)
  row['cleanup']=cleanup_group(p.pid)
 row['exit_code']=p.returncode;(b/(label+'.stdout')).write_bytes(out);(b/(label+'.stderr')).write_bytes(err);save()
 if require and (p.returncode or row.get('timed_out') or not row['cleanup']['clean_shutdown']):raise RuntimeError('failed '+label)
 return out.decode(errors='replace')
def cli(label,*args,**kw):return run(label,base+['docker',*args],**kw)
def free(host,udp=False):
 with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET,socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:s.bind((host,0));return s.getsockname()[1]
def echo(label,host,port,udp=False):
 for size in ([1,1200,1400,1452,1453,2000,8000,60000] if udp else [1,65536]):
  e={'label':label,'host':host,'port':port,'udp':udp,'size':size,'stage':'connect'};r['echoes'].append(e);t=time.monotonic();data=(bytes(range(256))*((size+255)//256))[:size]
  try:
   with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET,socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
    sock.settimeout(3);sock.connect((host,port));e['stage']='send';sock.sendall(data);e['stage']='recv';received=bytearray()
    while len(received)<size:
     chunk=sock.recv(65536 if udp else size-len(received))
     if not chunk:raise RuntimeError('early EOF')
     received.extend(chunk)
     if udp:break
    e['received_bytes']=len(received);e['verified']=bytes(received)==data
  except Exception as error:e['error']=repr(error);e['verified']=False
  e['elapsed_s']=time.monotonic()-t;save()
def dump(label):
 return cli(label,'run','--rm','--network','host','--pid','host','-v','/run:/host-run:ro','diag:local','/bin/busybox','sh','-c','/bin/busybox ip -6 addr show dev vec0; /bin/busybox ip -6 route; /bin/busybox ip -6 neigh; /bin/busybox cat /proc/net/tcp6 /proc/net/udp6; /bin/busybox ps w; /bin/busybox tail -80 /host-run/dockerd.log',timeout=45,require=False)
lock=(w/'kernel-build.lock').open('a');fcntl.flock(lock,fcntl.LOCK_SH)
try:
 files=[b/'network_backend.c',b/'client6.c',b/'client6',b/'rootfs.tar',w/'network-backend',repo/'scripts/harddind.py',repo/'scripts/port_driver.py',pathlib.Path('/root/harddind-rebuild/cpu-cpuset-kernel-v1'),pathlib.Path('/root/harddind-rebuild/publish-owner-v3/build/runtime-publish-v3.ext4')]
 r['sha256']={str(p):harddind.sha(p) for p in files};save()
 run('start',base+['start','--kernel','/root/harddind-rebuild/cpu-cpuset-kernel-v1','--disk','/root/harddind-rebuild/publish-owner-v3/build/runtime-publish-v3.ext4','--publish-ports','--cpus','4','--dns','1.1.1.1'],180)
 cli('import','import',str(b/'rootfs.tar'),'diag:local',timeout=120)
 ports={'v4tcp':free('127.0.0.1'),'v4udp':free('127.0.0.1',True),'v6tcp':free('::1'),'v6udp':free('::1',True)};r['ports']=ports
 for name in ports:
  proto='udp' if name.endswith('udp') else 'tcp';host='[::1]' if name.startswith('v6') else '127.0.0.1'
  cli('run-'+name,'run','-d','--name',name,'-p',f'{host}:{ports[name]}:7777/{proto}','diag:local','/netprobe',proto,'7777','0.0.0.0')
 r['forwards']=harddind.control(instance,'list');save();dump('guest-before')
 for name in ports:echo('outer-'+name,'::1' if name.startswith('v6') else '127.0.0.1',ports[name],name.endswith('udp'))
 dump('guest-after-outer')
 rows=r['forwards'];rows=rows if isinstance(rows,list) else rows.get('forwards',[])
 v6=next(x for x in rows if x.get('family')=='ipv6' and x['protocol']=='tcp') if any(x.get('family')=='ipv6' for x in rows) else next(x for x in rows if ':' in x.get('guest_address','') and x.get('protocol')=='tcp')
 cli('guest-direct-v6','run','--rm','--network','host','diag:local','/client6',v6['guest_address'],str(v6['guest_port']),timeout=20,require=False)
 cli('guest-ping-gateway','run','--rm','--network','host','diag:local','/bin/busybox','ping6','-c','1','-W','2','fd00::2',timeout=20,require=False)
 dump('guest-final')
 for name in ports:cli('logs-'+name,'logs',name,require=False)
 r['diagnostic_complete']=True
except BaseException as e:r['error']=repr(e)
finally:
 try:run('stop',base+['stop'],90,require=False);r['final_state']=harddind.read_state(instance)
 except BaseException as e:r['cleanup_error']=repr(e)
 r['status']='finished';r['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();r['sha256_after']={p:harddind.sha(pathlib.Path(p)) for p in r.get('sha256',{})};r['inputs_unchanged']=r.get('sha256')==r['sha256_after'];save()
