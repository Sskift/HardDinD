from pathlib import Path
import subprocess,json,datetime,os,traceback,hashlib
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';work=b/'run'
old=json.loads((w/'cpu-observer-owner-v1/report.json').read_text());assert old['status']=='finished' and old['normal_shutdown'] and old['inputs_unchanged']
assert json.loads((w/'cpu-observer-owner-v1/cleanup-audit.json').read_text())['all_empty']
state={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runs':{},'full_acceptance':False}
def save():(b/'runtime-status.json').write_text(json.dumps(state,indent=2)+'\n')
save();rc=1
try:
 for label,mode in [('mapping-features-v4','--system-features'),('mapping-alloc-v4','--alloc-object')]:
  cmd=['python3',str(repo/'scripts/run-gpu-control.py'),str(work),'--kernel',str(b/'kernel'),'--name',label,mode,'--async-wait']
  with (b/(label+'.log')).open('x') as log:
   p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
   state['runs'][label]={'pid':p.pid,'command':cmd};save();state['runs'][label]['exit_code']=p.wait();save()
  d=json.loads((work/'results'/label/'report.json').read_text())
  if not d.get('diagnostic_complete') or not all(v.get('cleanup',{}).get('clean_shutdown') for v in d['phases'].values()):raise RuntimeError(label+' incomplete or regression; preserve failure and stop before CUDA')
 label='mapping-cuda-v4';cmd=['python3',str(repo/'scripts/run-gpu-cuda.py'),str(work),'--kernel',str(b/'kernel'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name',label,'--async-wait']
 with (b/(label+'.log')).open('x') as log:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
  state['runs'][label]={'pid':p.pid,'command':cmd};save();state['runs'][label]['exit_code']=p.wait();save()
 d=json.loads((work/'results'/label/'report.json').read_text());assert d.get('normal_shutdown'), 'CUDA guest did not stop cleanly'
 rc=state['runs'][label]['exit_code']
except BaseException:state['error']=traceback.format_exc()
state.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
