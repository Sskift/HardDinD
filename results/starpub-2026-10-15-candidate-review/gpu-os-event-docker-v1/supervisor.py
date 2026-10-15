from pathlib import Path
import fcntl,hashlib,json,os,signal,subprocess,sys,time,traceback,tarfile,io,datetime
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';run=b/'run';name='os-events-v1';kernel=w/'gpu-mapping-owner-v13/kernel';disk=w/'runtime-shares-v1.ext4';probe=b/'gpu-os-event-probe'
sys.path.insert(0,str(repo/'scripts'));from guest_cleanup import cleanup_group,group_members
import harddind,gpu_os_event_checks as checks
r={'status':'running','stage':'waiting_for_prior','commands':[],'verdicts':{},'full_acceptance':False,'internal_cuda_verified':False,'performance_acceptance':False,'sources':{str(p.relative_to(repo)):harddind.sha(p) for p in repo.rglob('*') if p.is_file() and '__pycache__' not in p.parts},'binary_sha256':harddind.sha(probe),'kernel_sha256':harddind.sha(kernel),'disk_sha256':harddind.sha(disk)}
def save():
 p=b/'owner-status.tmp';p.write_text(json.dumps(r,indent=2)+'\n');p.replace(b/'owner-status.json')
def call(label,cmd,timeout=90):
 row={'label':label,'command':cmd};r['commands'].append(row);save()
 with (b/(label+'.stdout')).open('xb') as out,(b/(label+'.stderr')).open('xb') as err:
  p=subprocess.Popen(cmd,cwd=repo,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);row['pid']=p.pid;save()
  try:row['exit_code']=p.wait(timeout)
  except subprocess.TimeoutExpired:row['timed_out']=True;os.killpg(p.pid,signal.SIGKILL);row['exit_code']=p.wait(10)
  finally:row['cleanup']=cleanup_group(p.pid);save()
 return row
base=['python3',str(repo/'scripts/harddind.py'),str(run),name];rc=1;save();os.sched_setaffinity(0,{0,1,2,3})
try:
 deadline=time.monotonic()+1200
 while json.loads((w/'posix-focus-owner-v2/owner-status.json').read_text())['status']!='finished':
  if time.monotonic()>deadline:raise RuntimeError('prior focus deadline')
  time.sleep(1)
 assert json.loads((w/'posix-focus-owner-v2/owner-status.json').read_text())['groups_empty']
 with tarfile.open(b/'rootfs.tar','w') as tar:
  data=probe.read_bytes();i=tarfile.TarInfo('gpu-os-event-probe');i.mode=0o755;i.size=len(data);tar.addfile(i,io.BytesIO(data))
 for phase in ['native-before','docker','native-after']:
  r['stage']=phase;save()
  with (w/'kernel-build.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_SH if phase=='docker' else fcntl.LOCK_EX)
   try:
    if phase=='docker':
     assert call('start',base+['start','--kernel',str(kernel),'--disk',str(disk),'--gpu-control-experiment','--gpu-async-wait','--memfd-ram','--memfd-stub'],180)['exit_code']==0
     r['ready_state']=harddind.read_state(run/'instances'/name);save()
     assert call('import',base+['docker','import',str(b/'rootfs.tar'),'harddind-os-events:local'],120)['exit_code']==0
    for index in range(4):
     label=phase+'-'+str(index);device='/dev/nvidia'+str(index);args=['--device',device,'--ctl-device','/dev/nvidiactl','--poll-ms','30']
     cmd=[str(probe),*args] if phase!='docker' else base+['docker','run','--rm','--name','event-card-'+str(index),'--device=/dev/nvidiactl:/dev/nvidiactl','--device='+device+':'+device,'harddind-os-events:local','/gpu-os-event-probe',*args]
     row=call(label,cmd,45);v=checks.analyze((b/(label+'.stdout')).read_text(),{'device':device,'ctl_device':'/dev/nvidiactl','poll_ms':30});v['process_ok']=row['exit_code']==0 and not row.get('timed_out') and row['cleanup']['clean_shutdown'];r['verdicts'][label]=v;save()
   finally:
    if phase=='docker':
     for index in range(4):call('cleanup-card-'+str(index),base+['docker','rm','-f','event-card-'+str(index)],20)
     call('stop',base+['stop'],100);r['final_state']=harddind.read_state(run/'instances'/name);save()
 rc=0 if len(r['verdicts'])==12 and all(v['ok'] and v['process_ok'] for v in r['verdicts'].values()) else 1
except BaseException:r['error']=traceback.format_exc()
finally:
 audit={str(c['pid']):group_members(c['pid']) for c in r['commands'] if 'pid' in c};r['groups_empty']=all(v==([],[]) for v in audit.values());(b/'owner-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
 r['inputs_unchanged']=all(harddind.sha(repo/n)==h for n,h in r['sources'].items()) and harddind.sha(probe)==r['binary_sha256'] and harddind.sha(kernel)==r['kernel_sha256'] and harddind.sha(disk)==r['disk_sha256'];r.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
