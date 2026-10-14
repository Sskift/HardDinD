from pathlib import Path
import subprocess,json,datetime,os,fcntl,sys,hashlib,platform,traceback
b=Path(__file__).resolve().parent;w=b.parent;guest=w/'cpu-capture-owner-v1';sys.path.insert(0,str(guest/'repo/scripts'));from guest_cleanup import group_members,cleanup_group
assert json.loads((guest/'status.json').read_text())['status']=='finished'
g=json.loads((guest/'run/results/cpu-capture-v1/report.json').read_text());groups=[x['cleanup']['process_group'] for x in g['commands']]+[g['final_state'][k]['process_group'] for k in ('guest_cleanup','network_cleanup')];assert all(not any(group_members(pid)) for pid in groups)
s={'status':'running','supervisor_pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'native diagnostic only; host cgroup differs and includes other processes; no enforcement or performance acceptance','host':platform.uname()._asdict(),'affinity':sorted(os.sched_getaffinity(0)),'runs':[]};binary=guest/'validated-probe';s['binary_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest();s['source_sha256']=hashlib.sha256((guest/'repo/src/cpu_capture_probe.c').read_bytes()).hexdigest()
def save():(b/'report.json').write_text(json.dumps(s,indent=2)+'\n')
save();rc=1
try:
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_SH)
  for name,cpus in [('four_cpus','0-3'),('single_cpu','0')]:
   cmd=['taskset','-c',cpus,str(binary),'spin','--threads','4','--duration-ms','6000','--sample-ms','100']
   with (b/(name+'.stdout')).open('x') as out,(b/(name+'.stderr')).open('x') as err:
    p=subprocess.Popen(cmd,stdout=out,stderr=err,start_new_session=True);s['active_pid']=p.pid;save()
    timed_out=False
    try:code=p.wait(timeout=30)
    except subprocess.TimeoutExpired:timed_out=True;code=None
    cleanup=cleanup_group(p.pid)
   s['runs'].append({'name':name,'command':cmd,'pid':p.pid,'exit_code':code,'timed_out':timed_out,'cleanup':cleanup});save()
   assert code==0 and not timed_out and cleanup['clean_shutdown']
  rc=0
except BaseException:s['error']=traceback.format_exc()
s.pop('active_pid',None);s.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
