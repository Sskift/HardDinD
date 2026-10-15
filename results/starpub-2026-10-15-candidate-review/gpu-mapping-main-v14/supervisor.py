
from pathlib import Path
import json,hashlib,subprocess,tarfile,fcntl,sys,os,signal,traceback
b=Path(__file__).resolve().parent;w=b.parent;repo=b/'repo'
sys.path.insert(0,str(w/'gpu-mapping-owner-v14/repo/scripts'));from guest_cleanup import cleanup_group,group_members
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();r={'status':'running','full_acceptance':False}
def save():(b/'owner-status.json').write_text(json.dumps(r,indent=2)+'\n')
save();rc=1
try:
 with (w/'kernel-build.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  expected=json.loads((w/'gpu-mapping-owner-v14/preparation-status.json').read_text())['source_sha256_after']
  source=b/'linux-7.2.10';source.mkdir();archive=w/'cache/linux-7.2.10.tar.xz'
  with tarfile.open(archive,'r|*')as tar:
   for member in tar:
    name=member.name.removeprefix('linux-7.2.10/')
    if name in expected and member.isfile():
     p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(tar.extractfile(member).read())
  cmd=['python3',str(repo/'scripts/apply-kernel-patches.py'),str(source),str(archive),'--gpu-mapping-experiment'];r['command']=cmd
  with (b/'apply.stdout').open('x')as out,(b/'apply.stderr').open('x')as err:
   p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True);r['pid']=p.pid;save()
   try:rc=p.wait(180)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);rc=p.wait(15);raise
   finally:r['cleanup']=cleanup_group(p.pid);save()
  assert rc==0 and r['cleanup']['clean_shutdown']
  actual={n:sha(source/n)for n in expected};r['actual_files']=actual;r['expected_files']=expected;r['exact_match']=actual==expected;assert r['exact_match']
  r['runtime_config_matches']=sha(repo/'kernel/runtime.config')==sha(w/'gpu-mapping-owner-v14/repo/kernel/runtime.config');assert r['runtime_config_matches']
  r['groups_empty']=group_members(p.pid)==([],[]);assert r['groups_empty'];rc=0
except BaseException:r['error']=traceback.format_exc();rc=1
r.update(status='finished',exit_code=rc);save()
