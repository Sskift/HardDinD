#!/usr/bin/env python3
"""Compare real outer/native and Docker bind-mounted file semantics and I/O."""
import argparse
import ctypes
import errno
import fcntl
import io
import json
import os
import pathlib
import select
import shlex
import statistics
import subprocess
import sys
import tarfile
import time
import harddind


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--cache', choices=('auto', 'never'), default='auto')
    a = p.parse_args()
    if not a.name or len(a.name)>28 or not all(c.isalnum() or c in '-_' for c in a.name): p.error('invalid name')
    work=a.work.resolve(); repo=pathlib.Path(__file__).resolve().parents[1]; out=work/'results'; out.mkdir(exist_ok=True)
    result=out/(a.name+'.json'); logfile=out/(a.name+'.log'); share=work/'shares'/a.name
    instance=work/'instances'/a.name
    if any(x.exists() for x in [result, logfile, share, instance]): p.error('refusing existing state or evidence')
    share.mkdir(parents=True, mode=0o755); os.chmod(share,0o755)
    os.sched_setaffinity(0,[0,1,2,3])
    log=logfile.open('x'); base=[sys.executable,str(repo/'scripts/harddind.py'),str(work),a.name]
    names=['read_initial','outer_write_open_fd','outer_write_reopen','outer_truncate_fstat','outer_chmod_fstat',
           'guest_write_outer_read','mmap_outer_to_guest','mmap_guest_to_outer','replace_open_inode',
           'unlink_open_inode','xattr_both_directions','outer_lock_blocks_guest','guest_lock_blocks_outer',
           'outer_inotify_to_guest','guest_inotify_to_outer','user_permission_denied','user_create_owner','read_only_bind',
           'outer_inotify_to_guest_uid1001','guest_inotify_to_outer_uid1001','parallel_creation_masks']
    report={'kind':'shared_file_semantics_and_io','cache':a.cache,'full_acceptance':False,
            'checks':{mode:dict.fromkeys(names,'not_tested') for mode in ['native','docker']},'errors':{},'samples':[],
            'cpu_affinity':sorted(os.sched_getaffinity(0)),'cpu_exclusive':False,
            'native_read_only_baseline':'O_RDONLY descriptor; not a native read-only mount',
            'sha256':{str(f):harddind.sha(f) for f in [a.kernel,a.disk,work/'virtiofsd',work/'share-probe',
                         repo/'scripts/harddind.py',repo/'guest/runtime-init',repo/'src/share_probe.c',pathlib.Path(__file__)]},
            'not_tested':['cross-layer Unix sockets/FIFOs','bind propagation','crash recovery','ACL enforcement',
                          'overlay upperdir on shared filesystem','large directory and multi-client stress','GPU']}
    image='harddind-share-probe:local'; serial=0
    def run(args,timeout=180):
        log.write('$ '+shlex.join(args)+'\n');log.flush()
        r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
        text=r.stdout.decode(errors='replace');log.write(text+f'\n[exit={r.returncode}]\n');log.flush()
        if r.returncode:raise RuntimeError('command failed: '+shlex.join(args))
        return text
    def cli(*args):return run(base+['docker',*args])
    class Peer:
        def __init__(self,mode,path,uid=None,readonly=False):
            nonlocal serial
            serial+=1;self.mode=mode;self.name='fs-probe-'+str(serial);self.buffer=b''
            if mode=='native':
                cmd=[str(work/'share-probe'),path.name]+([str(uid)] if uid is not None else [])
            else:
                cmd=base+['docker','run','--rm','-i','--name',self.name]
                if uid is not None:cmd+=['--user',f'{uid}:{uid}']
                cmd+=['--mount',f'type=bind,src={share},dst=/share'+(',readonly' if readonly else ''),
                      image,'/share-probe','/share/'+path.name]
            log.write('$ '+shlex.join(cmd)+'\n');log.flush()
            self.proc=subprocess.Popen(cmd,cwd=share,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,bufsize=0,start_new_session=True)
            try:self.ready=self.receive()
            except BaseException:self.close();raise
            if not self.ready.get('ready'):raise RuntimeError('probe did not start')
        def receive(self):
            deadline=time.monotonic()+90
            while b'\n' not in self.buffer:
                remain=deadline-time.monotonic()
                if remain<=0 or not select.select([self.proc.stdout],[],[],remain)[0]:raise TimeoutError('probe response timed out')
                part=os.read(self.proc.stdout.fileno(),65536)
                if not part:raise RuntimeError('probe exited before response')
                self.buffer+=part
            line,self.buffer=self.buffer.split(b'\n',1)
            log.write(line.decode(errors='replace')+'\n');log.flush()
            return json.loads(line)
        def ask(self,command,ok=True):
            log.write('> '+command+'\n');log.flush()
            self.proc.stdin.write((command+'\n').encode());self.proc.stdin.flush();row=self.receive()
            if ok and row.get('rc')!=0:raise RuntimeError(str(row))
            return row
        def close(self):
            if self.proc.poll() is None:
                try:self.proc.stdin.write(b'quit\n');self.proc.stdin.close();self.proc.wait(timeout=15)
                except (OSError,subprocess.TimeoutExpired):self.proc.terminate();self.proc.wait(timeout=5)
            if self.mode=='docker':
                # A failed CLI connection must not leave its container running.
                r=subprocess.run(base+['docker','rm','-f',self.name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            self.proc.stdout.close()
            if self.proc.returncode:raise RuntimeError(f'probe exited {self.proc.returncode}')
        def __enter__(self):return self
        def __exit__(self,*unused):self.close()
    def host_write(path,byte):
        with path.open('r+b',buffering=0) as f:f.write(bytes([byte]));os.fsync(f.fileno())
    def expect_byte(peer,op,byte):
        row=peer.ask(op);assert row.get('byte')==byte,row
    def one(mode,name):
        path=share/(mode+'-'+name);path.write_bytes(b'A'*8192);os.chmod(path,0o666)
        uid=1001 if name in ['user_permission_denied','user_create_owner','outer_inotify_to_guest_uid1001'] else None
        if name=='user_permission_denied':os.chmod(path,0o600)
        if name=='user_create_owner':
            path.unlink();os.chmod(share,0o777)
        try:
            with Peer(mode,path,uid,readonly=name=='read_only_bind') as peer:
                if name=='user_permission_denied':
                    row=peer.ask('open',ok=False);assert row['rc']==-1 and row['errno']==errno.EACCES,row;return
                if name=='user_create_owner':
                    peer.ask('create');peer.ask('write 66');st=path.stat()
                    assert (st.st_uid,st.st_gid,st.st_mode&0o777)==(1001,1001,0o600),st
                    assert path.read_bytes()==b'B';return
                if name=='read_only_bind':
                    if mode=='native':
                        # The native baseline is an O_RDONLY descriptor, not a host mount.
                        peer.ask('open-read');row=peer.ask('write 66',ok=False)
                        assert row['rc']==-1 and row['errno']==errno.EBADF,row
                    else:
                        row=peer.ask('open',ok=False);assert row['rc']==-1 and row['errno']==errno.EROFS,row
                        peer.ask('open-read')
                    expect_byte(peer,'read',65);return
                if name=='parallel_creation_masks':peer.ask('parallel-create');return
                if name=='guest_inotify_to_outer_uid1001':
                    # Supplemental case; preserve the failed root watcher result.
                    with Peer('native',path,uid=1001) as watcher:
                        watcher.ask('watch');peer.ask('open');peer.ask('write 66');time.sleep(.1)
                        row=watcher.ask('events');assert row['bytes']>0,row
                    return
                peer.ask('open')
                if name=='read_initial':expect_byte(peer,'read',65)
                elif name in ['outer_write_open_fd','outer_write_reopen']:
                    expect_byte(peer,'read',65);host_write(path,66)
                    if name=='outer_write_reopen':peer.ask('close');peer.ask('open')
                    expect_byte(peer,'read',66)
                elif name=='outer_truncate_fstat':
                    peer.ask('stat');os.truncate(path,16384);row=peer.ask('stat');assert row['size']==16384,row
                elif name=='outer_chmod_fstat':
                    peer.ask('stat');os.chmod(path,0o640);row=peer.ask('stat');assert row['mode']==0o640,row
                elif name=='guest_write_outer_read':
                    peer.ask('write 66');assert path.read_bytes()[0]==66
                elif name.startswith('mmap_'):
                    peer.ask('map');expect_byte(peer,'mapped',65)
                    if name=='mmap_outer_to_guest':host_write(path,66);expect_byte(peer,'mapped',66)
                    else:peer.ask('mwrite 66');assert path.read_bytes()[0]==66
                    peer.ask('unmap')
                elif name=='replace_open_inode':
                    replacement=path.with_suffix('.new');replacement.write_bytes(b'B'*8192);replacement.replace(path)
                    expect_byte(peer,'read',65);peer.ask('close');peer.ask('open');expect_byte(peer,'read',66)
                elif name=='unlink_open_inode':
                    path.unlink();peer.ask('write 66');expect_byte(peer,'read',66)
                    path.write_bytes(b'C'*8192);peer.ask('close');peer.ask('open');expect_byte(peer,'read',67)
                elif name=='xattr_both_directions':
                    peer.ask('setxattr');assert os.getxattr(path,'user.harddind')==b'verified'
                    os.setxattr(path,'user.harddind',b'outer-ok');assert peer.ask('getxattr')['outer_value']
                elif name=='outer_lock_blocks_guest':
                    with path.open('r+b') as f:
                        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        row=peer.ask('lock',ok=False);peer.ask('unlock')
                        assert row['rc']==-1 and row['errno'] in (errno.EAGAIN,errno.EWOULDBLOCK),row
                elif name=='guest_lock_blocks_outer':
                    peer.ask('lock');blocked=False
                    with path.open('r+b') as f:
                        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError:blocked=True
                    peer.ask('unlock');assert blocked,'outer lock ignored guest lock'
                elif name in ['outer_inotify_to_guest','outer_inotify_to_guest_uid1001']:
                    peer.ask('watch');host_write(path,66);time.sleep(.1)
                    row=peer.ask('events');assert row['bytes']>0,row
                elif name=='guest_inotify_to_outer':
                    libc=ctypes.CDLL(None,use_errno=True);fd=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
                    if fd<0:raise OSError(ctypes.get_errno(),'inotify_init1')
                    try:
                        assert libc.inotify_add_watch(fd,os.fsencode(path),2)>=0
                        peer.ask('write 66');assert select.select([fd],[],[],2)[0],'no outer inotify event'
                        assert os.read(fd,4096)
                    finally:os.close(fd)
        finally:os.chmod(share,0o755)
    def suite(mode):
        for name in names:
            try:one(mode,name);report['checks'][mode][name]='pass'
            except BaseException as e:
                report['checks'][mode][name]='fail';report['errors'][mode+'/'+name]=f'{type(e).__name__}: {e}'
            print(mode+' '+name+' '+report['checks'][mode][name],flush=True)
    def bench(mode,phase):
        path=share/(mode+'-bench-'+phase);path.write_bytes(b'')
        with Peer(mode,path) as peer:
            peer.ask('open')
            for sample in range(3):
                for op in ['bench-write 32','bench-read 32']:
                    row=peer.ask(op);report['samples'].append({'mode':mode,'phase':phase,'sample':sample,**row})
    try:
        suite('native');bench('native','before')
        run(base+['start','--kernel',str(a.kernel.resolve()),'--disk',str(a.disk.resolve()),'--dns','1.1.1.1',
                  '--publish-ports','--share',str(share),'--share-cache',a.cache])
        archive=out/(a.name+'-rootfs.tar');program=(work/'share-probe').read_bytes()
        with tarfile.open(archive,'w') as tar:
            entry=tarfile.TarInfo('share-probe');entry.mode=0o755;entry.size=len(program);tar.addfile(entry,io.BytesIO(program))
        report['image_id']=cli('import',str(archive),image).strip()
        report['server']=json.loads(cli('version','--format','{{json .Server}}'))
        suite('docker');bench('docker','first');bench('docker','second')
        run(base+['stop'],timeout=70);report['runtime']=harddind.read_state(instance)
        bench('native','after')
    except BaseException as e:
        report['error']=f'{type(e).__name__}: {e}';print(report['error'],flush=True)
    finally:
        if harddind.live(harddind.read_state(instance)):
            try:run(base+['stop'],timeout=70)
            except BaseException as e:report['cleanup_error']=str(e)
        report['runtime']=harddind.read_state(instance)
        report['median_MiB_per_second']={mode:{op:statistics.median(x['MiB_per_second'] for x in report['samples'] if x['mode']==mode and x['op']==op)
            for op in ('bench-read','bench-write') if any(x['mode']==mode and x['op']==op for x in report['samples'])} for mode in ('native','docker')}
        report['all_checked_semantics_passed']=all(v=='pass' for group in report['checks'].values() for v in group.values())
        result.write_text(json.dumps(report,indent=2)+'\n');log.close()
    print(json.dumps({'result':str(result),'checks':report['checks'],'full_acceptance':False},indent=2))
    return 0 if report['all_checked_semantics_passed'] and not report.get('error') and report['runtime'].get('phase')=='stopped' else 1


if __name__=='__main__':raise SystemExit(main())
