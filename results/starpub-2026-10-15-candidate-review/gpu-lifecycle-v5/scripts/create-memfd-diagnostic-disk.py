"""Clone only the private fixture; retain full daemon diagnostics on failure."""
import fcntl
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile

work=pathlib.Path('/root/harddind-rebuild')
base=work/'docker-moby-context.ext4'
disk=work/'docker-memfd-diagnostic-v1.ext4'
init=work/'tests/docker-init'
report=work/'results/memfd-docker-fixture-v1.json'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
with (work/'kernel-build.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    if disk.exists() or report.exists():raise RuntimeError('refusing existing fixture')
    before=sha(base)
    subprocess.run(['cp','--reflink=auto','--sparse=always',str(base),str(disk)],check=True)
    commands=['rm /sbin/harddind-docker-init',f'write {init} /sbin/harddind-docker-init',
              'set_inode_field /sbin/harddind-docker-init mode 0100755']
    results=[]
    for command in commands:
        r=subprocess.run(['debugfs','-w','-R',command,str(disk)],capture_output=True,text=True,check=True)
        results.append({'command':command,'stdout':r.stdout,'stderr':r.stderr})
    with tempfile.TemporaryDirectory(dir=work/'tmp') as directory:
        copy=pathlib.Path(directory)/'init'
        subprocess.run(['debugfs','-R',f'dump /sbin/harddind-docker-init {copy}',str(disk)],capture_output=True,check=True)
        if copy.read_bytes()!=init.read_bytes():raise RuntimeError('init contents differ')
    r=subprocess.run(['debugfs','-R','stat /sbin/harddind-docker-init',str(disk)],capture_output=True,text=True,check=True)
    if not re.search(r'Mode:\s+0755\b',r.stdout):raise RuntimeError(r.stdout)
    fsck=subprocess.run(['e2fsck','-fn',str(disk)],capture_output=True,text=True,check=True)
    if sha(base)!=before:raise RuntimeError('base disk changed')
    record={'base':str(base),'base_sha256':before,'disk':str(disk),'disk_sha256':sha(disk),
            'init_sha256':sha(init),'source_sha256':sha(pathlib.Path(__file__)),
            'commands':results,'inode_stat':r.stdout,'fsck':fsck.stdout+fsck.stderr,'verified':True}
    with report.open('x') as f:f.write(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
