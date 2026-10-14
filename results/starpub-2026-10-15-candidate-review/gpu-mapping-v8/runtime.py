from pathlib import Path
import json,os,subprocess,datetime,traceback
b=Path(__file__).resolve().parent;w=b.parent
r={'status':'running','pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'full_acceptance':False}
def save():(b/'runtime-status.json').write_text(json.dumps(r,indent=2)+'\n')
save()
try:
 cmd=['python3',str(b/'repo/scripts/run-gpu-cuda.py'),str(b/'run'),'--kernel',str(b/'kernel'),'--disk',str(w/'runtime-shares-v1.ext4'),'--name','mapping-cuda-v8','--async-wait']
 with (b/'mapping-cuda-v8.log').open('x') as out:
  p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT);r['child_pid']=p.pid;r['command']=cmd;save();r['exit_code']=p.wait()
 d=json.loads((b/'run/results/mapping-cuda-v8/report.json').read_text());assert d.get('normal_shutdown'),'guest did not stop normally'
except BaseException:r['error']=traceback.format_exc();r['exit_code']=1
r.update(status='finished',finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
