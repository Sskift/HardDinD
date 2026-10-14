from pathlib import Path
import subprocess,json,datetime,os,traceback,hashlib
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo';work=b/'run'
sys_path=str(repo/'scripts')
import sys
sys.path.insert(0,sys_path)
from guest_cleanup import group_members
prior=json.loads((w/'fp-matrix-owner-v3/owner-status.json').read_text());assert prior['status']=='finished' and prior['all_cells_passed'] and prior['groups_empty'] and prior['inputs_unchanged']
assert all(group_members(int(g))==([],[]) for g in json.loads((w/'fp-matrix-owner-v3/owner-cleanup-audit.json').read_text()))
state={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runs':{},'full_acceptance':False}
def save():(b/'runtime-status.json').write_text(json.dumps(state,indent=2)+'\n')
save();rc=1
try:
 for label,mode in [('mapping-features-v13','--system-features'),('mapping-alloc-v13','--alloc-object')]:
  cmd=['python3',str(repo/'scripts/run-gpu-control.py'),str(work),'--kernel',str(b/'kernel'),'--name',label,mode,'--async-wait']
  with (b/(label+'.log')).open('x') as log:
   p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
   state['runs'][label]={'pid':p.pid,'command':cmd};save();state['runs'][label]['exit_code']=p.wait();save()
  d=json.loads((work/'results'/label/'report.json').read_text())
  if not d.get('diagnostic_complete') or not all(v.get('cleanup',{}).get('clean_shutdown') for v in d['phases'].values()):raise RuntimeError(label+' incomplete or regression; preserve failure and stop before CUDA')
 label='mapping-cuda-v13';cmd=['python3',str(repo/'scripts/run-gpu-cuda.py'),str(work),'--kernel',str(b/'kernel'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name',label,'--async-wait']
 with (b/(label+'.log')).open('x') as log:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
  state['runs'][label]={'pid':p.pid,'command':cmd};save();state['runs'][label]['exit_code']=p.wait();save()
 d=json.loads((work/'results'/label/'report.json').read_text());assert d.get('normal_shutdown'), 'CUDA guest did not stop cleanly'
 rc=state['runs'][label]['exit_code']
except BaseException:state['error']=traceback.format_exc()
def groups(value):
 out=set()
 if isinstance(value,dict):
  if 'process_group' in value and 'clean_shutdown' in value:out.add(value['process_group'])
  for child in value.values():out.update(groups(child))
 elif isinstance(value,list):
  for child in value:out.update(groups(child))
 return out
all_groups=set()
for entry in (work/'results').glob('*/report.json'):all_groups.update(groups(json.loads(entry.read_text())))
prep=json.loads((b/'preparation-status.json').read_text());all_groups.update(groups(prep))
audit={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'groups':{str(g):group_members(g) for g in sorted(all_groups)}}
audit['all_empty']=all(not live and not dead for live,dead in audit['groups'].values())
(b/'runtime-cleanup-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
state['groups_empty']=audit['all_empty'];state['inputs_unchanged']=all(hashlib.sha256((b/n).read_bytes()).hexdigest()==h for n,h in json.loads((b/'runtime-inputs.json').read_text()).items())
if not state['groups_empty'] or not state['inputs_unchanged']:rc=1
state.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
