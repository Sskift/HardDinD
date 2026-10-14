#!/usr/bin/env python3
"""Native vs real Docker tests for shared-file lock ownership and cancellation."""
import argparse
import contextlib
import errno
import fcntl
import io
import json
import os
import pathlib
import re
import select
import shlex
import signal
import statistics
import subprocess
import sys
import tarfile
import time
import harddind
from guest_cleanup import cleanup_group


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('work',type=pathlib.Path)
    p.add_argument('--kernel',type=pathlib.Path,required=True)
    p.add_argument('--disk',type=pathlib.Path,required=True)
    p.add_argument('--name',required=True)
    p.add_argument('--virtiofsd',type=pathlib.Path)
    p.add_argument('--inline-flock',action='store_true')
    p.add_argument('--functional-only',action='store_true',help='functional regression without timing samples')
    p.add_argument('--benchmark-only',action='store_true',help='diagnostic timings only; functional checks remain not_tested')
    p.add_argument('--profile',action='store_true',help='diagnostic virtiofs timing; not an uninstrumented performance run')
    p.add_argument('--cancel-race-iterations',type=int,default=0,help='additional timer/unlock races per mode; default disabled')
    p.add_argument('--posix-locks',action='store_true',help='enable the extended cross-layer POSIX byte-range matrix (needs --shared-posix-locks backend)')
    a=p.parse_args()
    if a.functional_only and a.benchmark_only:p.error('functional-only conflicts with benchmark-only')
    if not 0<=a.cancel_race_iterations<=1000:p.error('cancel-race-iterations must be between 0 and 1000')
    if a.cancel_race_iterations and a.benchmark_only:p.error('cancellation races require functional checks')
    if not a.name or len(a.name)>28 or not all(c.isalnum() or c in '-_' for c in a.name):p.error('invalid name')
    work=a.work.resolve();repo=pathlib.Path(__file__).resolve().parents[1];out=work/'results';out.mkdir(exist_ok=True)
    share=work/'shares'/a.name;instance=work/'instances'/a.name;result=out/(a.name+'.json');logfile=out/(a.name+'.log')
    if any(x.exists() for x in [share,instance,result,logfile]):p.error('refusing existing state or evidence')
    a.virtiofsd=(a.virtiofsd or work/'virtiofsd').resolve()
    share.mkdir(parents=True);os.sched_setaffinity(0,[0,1,2,3]);log=logfile.open('x')
    base=[sys.executable,str(repo/'scripts/harddind.py'),str(work),a.name];image='harddind-lock-probe:local';serial=0
    flock_names=['outer_excludes_inner','inner_excludes_outer','shared_readers','independent_opens','dup_last_close',
           'close_dup_keeps_lock','fork_last_close','close_releases','kill_releases','blocking_inner','blocking_outer',
           'signal_cancels_wait','kill_cancels_wait','six_waiters_no_starvation','rename_keeps_lock','unlink_keeps_lock',
           'downgrade','upgrade_failure_drops_shared','readonly_bind_flock','posix_outer_excludes_inner','posix_inner_excludes_outer']
    posix_names=['posix_one_byte_zero','posix_non_overlapping','posix_readers_coexist','posix_independent_guest_processes',
           'posix_self_multi_open','posix_close_dup_releases','posix_fork_close','posix_partial_unlock',
           'posix_downgrade_upgrade','posix_to_eof','posix_blocking_grant','posix_signal_eintr',
           'posix_kill_release','posix_whence_cur_end','posix_whence_before_bof','posix_getlk_reports',
           'posix_getlk_child_exact_pid','posix_getlk_host_pid_zero','posix_deadlock_cycle',
           'posix_two_file_deadlock_cycle',
           'posix_exec_preserves','posix_exec_missing','posix_readonly_rights','posix_cancel_grant_race',
           'posix_large_offsets','posix_rename_unlink']
    names=flock_names+(posix_names if a.posix_locks else [])
    if a.cancel_race_iterations:names.append('cancel_release_races')
    def host_state():
        return {name:pathlib.Path(path).read_text() for name,path in {
            'loadavg':'/proc/loadavg','cpu_pressure':'/sys/fs/cgroup/cpu.pressure',
            'cpu_stat':'/sys/fs/cgroup/cpu.stat','memory_events':'/sys/fs/cgroup/memory.events'}.items()}
    report={'kind':'shared_flock_lifecycle','full_acceptance':False,'instrumented':a.profile,
            'inline_flock':a.inline_flock,'posix_locks':a.posix_locks,'functional_only':a.functional_only,'benchmark_only':a.benchmark_only,'host_before':host_state(),
            'cancel_race_iterations':a.cancel_race_iterations,'cancel_races':{},
            'checks':{mode:dict.fromkeys(names,'not_tested') for mode in ('native','docker')},'errors':{},'samples':[],
            'peers':[],'cpu_affinity':sorted(os.sched_getaffinity(0)),'cpu_exclusive':False,
            'sha256':{str(f):harddind.sha(f) for f in [a.kernel,a.disk,a.virtiofsd,work/'share-probe',
                repo/'scripts/harddind.py',repo/'src/share_probe.c',pathlib.Path(__file__)]},
            'limitations':['native read-only baseline uses O_RDONLY, not a read-only mount',
                           'POSIX byte-range coverage runs only with --posix-locks and a backend advertising FUSE_POSIX_LOCKS; without it the two legacy cases must still fail rather than fall back silently',
                           'GETLK: guest holders report exact tgid within the mount pid namespace; cross-pid-namespace holders and host-process holders report 0 (no namespace translation)',
                           'deadlock detection covers guest-owner cycles across files; mixed host/guest blocking cycles are not detectable from userspace polling',
                           'mmap, notifications, migration, device reset, multiple exports and GPU are not accepted here']}
    def run(args,timeout=90):
        log.write('$ '+shlex.join(args)+'\n');log.flush()
        r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
        log.write(r.stdout.decode(errors='replace')+f'\n[exit={r.returncode}]\n');log.flush()
        if r.returncode:raise RuntimeError('command failed: '+shlex.join(args))
        return r.stdout.decode(errors='replace')
    def cli(*args):return run(base+['docker',*args])
    class Peer:
        def __init__(self,mode,path,readonly=False):
            nonlocal serial
            serial+=1;self.mode=mode;self.name=f'lock-probe-{serial}';self.buffer=b'';self.expected_kill=False;self.closed=False
            cmd=[str(work/'share-probe'),path.name] if mode=='native' else base+['docker','run','--rm','-i','--name',self.name,
                '--mount',f'type=bind,src={share},dst=/share'+(',readonly' if readonly else ''),image,'/share-probe','/share/'+path.name]
            log.write('$ '+shlex.join(cmd)+'\n');log.flush()
            self.proc=subprocess.Popen(cmd,cwd=share,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,bufsize=0,start_new_session=True)
            try:
                row=self.receive();assert row.get('ready'),row
            except BaseException:self.close();raise
        def receive(self,timeout=30):
            deadline=time.monotonic()+timeout
            while b'\n' not in self.buffer:
                remain=deadline-time.monotonic()
                if remain<=0 or not select.select([self.proc.stdout],[],[],remain)[0]:raise TimeoutError('probe response timeout')
                chunk=os.read(self.proc.stdout.fileno(),65536)
                if not chunk:raise RuntimeError('probe exited before reply')
                self.buffer+=chunk
            line,self.buffer=self.buffer.split(b'\n',1);log.write(line.decode()+'\n');log.flush();return json.loads(line)
        def send(self,command):
            log.write(self.name+'> '+command+'\n');log.flush();self.proc.stdin.write((command+'\n').encode())
        def ask(self,command,ok=True):
            self.send(command);row=self.receive()
            if ok:assert row.get('rc')==0,row
            return row
        def pending(self):
            assert not self.buffer and not select.select([self.proc.stdout],[],[],.15)[0], 'wait returned before unlock/cancel'
        def interrupt(self):
            if self.mode=='native':self.proc.send_signal(signal.SIGUSR1)
            else:cli('kill','--signal=USR1',self.name)
        def kill(self):
            self.expected_kill=True
            if self.mode=='native':self.proc.kill()
            else:cli('kill','--signal=KILL',self.name)
            code=self.proc.wait(timeout=15)
            assert code in (-signal.SIGKILL,137),code
        def close(self):
            if self.closed:return
            self.closed=True;forced=False
            try:
                if self.proc.poll() is None:
                    try:self.send('quit');self.proc.stdin.close();self.proc.wait(timeout=8)
                    except (OSError,subprocess.TimeoutExpired):
                        forced=True
                        if self.mode=='docker':
                            subprocess.run(base+['docker','rm','-f',self.name],stdout=subprocess.DEVNULL,stderr=log,timeout=15)
                        if self.proc.poll() is None:os.killpg(self.proc.pid,signal.SIGKILL)
                        self.proc.wait(timeout=5)
            finally:
                self.proc.stdout.close()
                if not self.proc.stdin.closed:self.proc.stdin.close()
                audit=cleanup_group(self.proc.pid)
                report['peers'].append({'name':self.name,'mode':self.mode,'exit_code':self.proc.returncode,
                                        'expected_kill':self.expected_kill,'forced':forced,**audit})
            assert not forced and audit['clean_shutdown'],'peer cleanup failed'
            assert self.expected_kill or self.proc.returncode==0,self.proc.returncode
        def __enter__(self):return self
        def __exit__(self,*unused):self.close()
    def blocked(peer,op='lock'):
        row=peer.ask(op,ok=False);assert row['rc']==-1 and row['errno'] in (errno.EAGAIN,errno.EACCES),row
    def outer_lock(file,should_block,shared=False,posix=False):
        lockfn=fcntl.lockf if posix else fcntl.flock
        operation=(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB
        got=True
        try:
            if posix:lockfn(file,operation,64,0)
            else:lockfn(file,operation)
        except BlockingIOError:got=False
        if got:
            if posix:lockfn(file,fcntl.LOCK_UN,64,0)
            else:lockfn(file,fcntl.LOCK_UN)
        assert got!=should_block,{'expected_blocked':should_block,'acquired':got}
    def posix_range(file,start,length,should_block,shared=False):
        """Try a host-process POSIX range lock and KEEP it when acquired."""
        operation=(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB
        got=True
        try:fcntl.lockf(file,operation,length,start,os.SEEK_SET)
        except BlockingIOError:got=False
        assert got!=should_block,{'expected_blocked':should_block,'acquired':got,'start':start,'length':length}
    def posix_unlock(file,start,length):
        fcntl.lockf(file,fcntl.LOCK_UN,length,start,os.SEEK_SET)
    def peer_denied(peer,command):
        row=peer.ask(command,ok=False)
        assert row['rc']==-1 and row['errno'] in (errno.EAGAIN,errno.EACCES),row
    def case(mode,name):
        path=share/(mode+'-'+name);path.write_bytes(b'A'*8192);os.chmod(path,0o666)
        with contextlib.ExitStack() as stack:
            peer=stack.enter_context(Peer(mode,path,readonly=name=='readonly_bind_flock'))
            peer.ask('open-read' if name=='readonly_bind_flock' else 'open')
            outer=stack.enter_context(path.open('r+b',buffering=0))
            if name=='outer_excludes_inner':
                fcntl.flock(outer,fcntl.LOCK_EX);blocked(peer);fcntl.flock(outer,fcntl.LOCK_UN);peer.ask('lock')
            elif name in ('inner_excludes_outer','readonly_bind_flock'):
                peer.ask('lock');outer_lock(outer,True);peer.ask('unlock');outer_lock(outer,False)
            elif name=='shared_readers':
                peer.ask('lock-shared');fcntl.flock(outer,fcntl.LOCK_SH|fcntl.LOCK_NB)
                other=stack.enter_context(Peer(mode,path));other.ask('open');other.ask('lock-shared')
                contender=stack.enter_context(path.open('r+b'));outer_lock(contender,True)
            elif name=='independent_opens':
                peer.ask('lock');peer.ask('open-second');blocked(peer,'lock-second');peer.ask('unlock');peer.ask('lock-second')
                outer_lock(outer,True);peer.ask('close-second');outer_lock(outer,False)
            elif name=='dup_last_close':
                peer.ask('lock');peer.ask('dup');peer.ask('close');outer_lock(outer,True)
                peer.ask('close-second');outer_lock(outer,False)
            elif name=='close_dup_keeps_lock':
                peer.ask('lock');peer.ask('dup');peer.ask('close-second');outer_lock(outer,True)
                peer.ask('close');outer_lock(outer,False)
            elif name=='fork_last_close':
                peer.ask('lock');peer.ask('fork-hold');peer.ask('close');outer_lock(outer,True)
                peer.ask('release-child');outer_lock(outer,False)
            elif name in ('close_releases','kill_releases'):
                peer.ask('lock');outer_lock(outer,True)
                peer.close() if name=='close_releases' else peer.kill()
                outer_lock(outer,False)
            elif name in ('blocking_inner','signal_cancels_wait','kill_cancels_wait'):
                peer.ask('signal-ready');fcntl.flock(outer,fcntl.LOCK_EX);peer.send('wait');peer.pending()
                if name=='blocking_inner':
                    fcntl.flock(outer,fcntl.LOCK_UN);row=peer.receive();assert row['rc']==0,row;outer_lock(outer,True)
                elif name=='signal_cancels_wait':
                    peer.interrupt();row=peer.receive();assert row['rc']==-1 and row['errno']==errno.EINTR,row
                    fcntl.flock(outer,fcntl.LOCK_UN);time.sleep(.1);outer_lock(outer,False)
                    peer.ask('lock');peer.ask('unlock')
                else:
                    peer.kill();fcntl.flock(outer,fcntl.LOCK_UN);time.sleep(.1);outer_lock(outer,False)
            elif name=='blocking_outer':
                peer.ask('lock');other=stack.enter_context(Peer('native',path));other.ask('open');other.send('wait');other.pending()
                peer.ask('unlock');row=other.receive();assert row['rc']==0,row;blocked(peer)
            elif name=='six_waiters_no_starvation':
                peer.ask('lock');waiting=[]
                for _ in range(6):
                    other=stack.enter_context(Peer(mode,path));other.ask('open');other.send('wait');other.pending();waiting.append(other)
                # This must run while all six waiters remain blocked; the backend has four workers.
                assert peer.ask('read')['byte']==65
                peer.ask('unlock')
                while waiting:
                    ready,_,_=select.select([q.proc.stdout for q in waiting],[],[],15)
                    assert ready,'all lock waiters stalled'
                    for other in list(waiting):
                        if other.proc.stdout in ready:
                            row=other.receive();assert row['rc']==0,row;other.ask('unlock');waiting.remove(other)
            elif name in ('rename_keeps_lock','unlink_keeps_lock'):
                peer.ask('lock')
                if name=='rename_keeps_lock':path.rename(path.with_suffix('.moved'))
                else:path.unlink()
                outer_lock(outer,True);peer.ask('close');outer_lock(outer,False)
            elif name=='downgrade':
                peer.ask('lock');peer.ask('lock-shared');outer_lock(outer,False,shared=True);outer_lock(outer,True)
            elif name=='upgrade_failure_drops_shared':
                peer.ask('lock-shared');fcntl.flock(outer,fcntl.LOCK_SH);blocked(peer)
                fcntl.flock(outer,fcntl.LOCK_UN);outer_lock(outer,False)
            elif name=='posix_outer_excludes_inner':
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,64,0);blocked(peer,'posix-lock')
            elif name=='posix_inner_excludes_outer':
                peer.ask('posix-lock');outer_lock(outer,True,posix=True)
            elif name=='posix_one_byte_zero':
                peer.ask('posix-wlock 0 1')
                posix_range(outer,0,1,True)
                posix_range(outer,1,1,False);posix_unlock(outer,1,1)
                peer.ask('posix-unlock 0 1')
            elif name=='posix_whence_before_bof':
                # Signed SEEK_END resolving before byte zero is EINVAL.
                row=peer.ask('posix-wlock-end -8193 1',ok=False)
                assert row['rc']==-1 and row['errno']==errno.EINVAL,row
            elif name=='posix_getlk_child_exact_pid':
                # Forked child shares the mount pid namespace but has its own
                # files_struct (a distinct FUSE owner). GETLK must report the
                # EXACT child tgid in both native and docker modes.
                row=peer.ask('fork-locker 100 64')
                child_pid=row['child_pid']
                reported=peer.ask('posix-getlk 1 0 4096')
                assert reported['type']==fcntl.F_WRLCK and reported['start']==100 and reported['len']==64,reported
                assert reported['pid']==child_pid,('exact pid mismatch',reported,child_pid)
                peer.ask('release-child')
            elif name=='posix_getlk_host_pid_zero':
                # A host-process holder is the driver process in native mode:
                # the kernel reports its real tgid.  In Docker mode the holder
                # lives in a different pid namespace and is translated to 0.
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,64,300,os.SEEK_SET)
                row=peer.ask('posix-getlk 1 256 4096')
                assert row['type']==fcntl.F_WRLCK and row['start']==300 and row['len']==64,row
                if mode=='native':
                    assert row['pid']==os.getpid(),('native host holder pid',row,os.getpid())
                else:
                    assert row['pid']==0,('cross-namespace host holder must report 0',row)
                fcntl.lockf(outer,fcntl.LOCK_UN,64,300,os.SEEK_SET)
            elif name=='posix_deadlock_cycle':
                # Bounded two-owner cycle; both kernel local detection and the
                # daemon waiter graph must return EDEADLK promptly, never hang.
                other=stack.enter_context(Peer(mode,path));other.ask('open')
                peer.ask('posix-wlock 0 64')
                other.ask('posix-wlock 64 64')
                peer.send('posix-wlock-w 64 64');peer.pending()
                other.send('posix-wlock-w 0 64')
                row=other.receive(timeout=10)
                assert row['rc']==-1 and row['errno']==errno.EDEADLK,row
                other.ask('posix-unlock 64 64')
                row=peer.receive(timeout=10);assert row['rc']==0,row
                peer.ask('posix-unlock 64 64');peer.ask('posix-unlock 0 64')
            elif name=='posix_two_file_deadlock_cycle':
                # Cross-file ABBA between two guest peers. The file Y is a
                # sibling so each peer's primary file is its held file and the
                # 'y' descriptor is the contended other file.
                ypath=path.with_name(path.name+'-y');ypath.write_bytes(b'A'*8192);os.chmod(ypath,0o666)
                other=stack.enter_context(Peer(mode,ypath))
                peer.ask('open');other.ask('open')
                peer.ask('other-open '+ypath.name)
                other.ask('other-open '+path.name)
                peer.ask('posix-wlock 0 64')
                other.ask('posix-wlock 0 64')
                peer.send('posix-wlock-wy 0 64');peer.pending()
                other.send('posix-wlock-wy 0 64')
                row=other.receive(timeout=10)
                assert row['rc']==-1 and row['errno']==errno.EDEADLK,row
                other.ask('posix-unlock 0 64')
                row=peer.receive(timeout=10);assert row['rc']==0,row
                peer.ask('posix-unlocky 0 64');peer.ask('posix-unlock 0 64')
                # Non-cycle one-way wait grants on unlock (bounded, no timeout PASS).
                peer.ask('posix-wlock 128 64')
                other.send('posix-wlock-wy 128 64');other.pending()
                peer.ask('posix-unlock 128 64')
                row=other.receive(timeout=10);assert row['rc']==0,row
                other.ask('posix-unlocky 128 64')
            elif name=='posix_exec_preserves':
                # Lock survives execve, proven post-exec by the self-exec
                # helper (the minimal image has no /bin/sleep): same pid, lock
                # fd open, marker emitted after exec; second guest process
                # denied until controlled reap.
                verifier=stack.enter_context(Peer(mode,path));verifier.ask('open')
                row=peer.ask('exec-hold 0 64')
                assert row.get('post_exec'),row
                assert row['reported_pid']==row['child_pid'] and row['lock_fd']>=0,row
                assert verifier.ask('posix-wlock 0 64',ok=False)['errno']==errno.EAGAIN
                reap=peer.ask('exec-reap')
                assert reap['rc']==0 and reap['exited'] and not reap['signaled'],reap
                assert reap['exit_code']==0 and reap['raw_status']==0,reap
                verifier.ask('posix-wlock 0 64');verifier.ask('posix-unlock 0 64')
            elif name=='posix_exec_missing':
                # Negative: absent helper target -> ENOENT, exit 127; no lock
                # may survive the failed exec child in the minimal image.
                verifier=stack.enter_context(Peer(mode,path));verifier.ask('open')
                row=peer.ask('exec-missing 200 64',ok=False)
                assert row['rc']==-1 and row['errno']==errno.ENOENT,row
                assert row['exited'] and row['exit_code']==127 and not row['signaled'],row
                verifier.ask('posix-wlock 200 64');verifier.ask('posix-unlock 200 64')
            elif name=='posix_readonly_rights':
                peer.ask('close');peer.ask('open-read')
                # Write lock on an O_RDONLY fd is EBADF before the wire.
                row=peer.ask('posix-wlock 0 64',ok=False)
                assert row['rc']==-1 and row['errno']==errno.EBADF,row
                # Read lock works and excludes host writers.
                peer.ask('posix-rlock 0 64')
                posix_range(outer,0,64,True)
                f2=stack.enter_context(path.open('rb',buffering=0))
                try:
                    fcntl.lockf(f2,fcntl.LOCK_SH|fcntl.LOCK_NB,64,0);got=True
                except BlockingIOError:got=False
                assert got,'read locks must share'
                peer.ask('posix-unlock 0 64')
            elif name=='posix_cancel_grant_race':
                # Deterministic outcomes; exclusion is proven by a SECOND
                # guest process (same-process fds share POSIX locks), never by
                # an unlock after EINTR that could mask a leaked acquisition.
                verifier=stack.enter_context(Peer(mode,path));verifier.ask('open')
                # (a) INTERRUPT: timer fires while the outer holder still holds.
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,64,128)
                peer.send('posix-timed-wait 128 64 5000')
                armed=peer.receive(timeout=5);assert armed.get('armed') is True,armed
                row=peer.receive(timeout=10)
                assert row['rc']==-1 and row['errno']==errno.EINTR,row
                assert verifier.ask('posix-wlock 128 64',ok=False)['errno']==errno.EAGAIN
                fcntl.lockf(outer,fcntl.LOCK_UN,64,128)
                # The interrupted peer never acquired: verifier gets it now.
                verifier.ask('posix-wlock 128 64');verifier.ask('posix-unlock 128 64')
                # (b) GRANT: unlock well before the 5 s timer; the granted peer
                # excludes the second guest process until explicit release.
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,64,256)
                peer.send('posix-timed-wait 256 64 5000000')
                armed=peer.receive(timeout=5);assert armed.get('armed') is True,armed
                fcntl.lockf(outer,fcntl.LOCK_UN,64,256)
                row=peer.receive(timeout=10)
                assert row['op']=='posix-timed-wait' and row['rc']==0,row
                assert verifier.ask('posix-wlock 256 64',ok=False)['errno']==errno.EAGAIN
                peer.ask('posix-unlock 256 64')
                verifier.ask('posix-wlock 256 64');verifier.ask('posix-unlock 256 64')
            elif name=='posix_large_offsets':
                hi=1<<62;last=(1<<63)-1
                peer.ask(f'posix-wlock {hi} 8')
                posix_range(outer,hi,8,True)
                posix_range(outer,hi-1,1,False);posix_unlock(outer,hi-1,1)
                peer.ask(f'posix-unlock {hi} 8')
                peer.ask(f'posix-wlock {last} 1')
                posix_range(outer,last,1,True)
                posix_range(outer,last-1,1,False);posix_unlock(outer,last-1,1)
                peer.ask(f'posix-unlock {last} 1')
                peer.ask(f'posix-wlock {hi} 0')
                posix_range(outer,last,1,True)
                posix_range(outer,hi-1,1,False);posix_unlock(outer,hi-1,1)
                other=stack.enter_context(Peer(mode,path));other.ask('open')
                row=other.ask(f'posix-getlk 1 0 0')
                assert row['type']==fcntl.F_WRLCK and row['start']==hi and row['len']==0,row
                peer.ask(f'posix-unlock {hi} 0')
                posix_range(outer,last,1,False)
            elif name=='posix_non_overlapping':
                peer.ask('posix-wlock 0 64')
                posix_range(outer,64,64,False)
                posix_range(outer,32,64,True)
                posix_unlock(outer,64,64)
                peer.ask('posix-unlock 0 64')
            elif name=='posix_readers_coexist':
                peer.ask('posix-rlock 0 64')
                other=stack.enter_context(Peer(mode,path));other.ask('open');other.ask('posix-rlock 0 64')
                posix_range(outer,0,64,False,shared=True)
                posix_unlock(outer,0,64)
                posix_range(outer,0,64,True)
                posix_unlock(outer,0,64)
            elif name=='posix_independent_guest_processes':
                peer.ask('posix-wlock 0 64')
                other=stack.enter_context(Peer(mode,path));other.ask('open')
                peer_denied(other,'posix-wlock 0 64')
                peer_denied(other,'posix-rlock 0 64')
                other.ask('posix-wlock 64 64')
            elif name=='posix_self_multi_open':
                peer.ask('posix-wlock 0 64');peer.ask('open-second');peer.ask('posix-wlock2 0 64')
                posix_range(outer,0,64,True)
                peer.ask('close-second');posix_range(outer,0,64,False);posix_unlock(outer,0,64)
                peer.ask('posix-wlock 0 64');posix_range(outer,0,64,True)
                peer.ask('close');posix_unlock(outer,0,64);posix_range(outer,0,64,False)
            elif name=='posix_close_dup_releases':
                peer.ask('posix-wlock 0 64');peer.ask('dup');peer.ask('close')
                posix_range(outer,0,64,False);posix_unlock(outer,0,64)
                peer.ask('posix-wlock2 0 64');posix_range(outer,0,64,True)
                peer.ask('close-second');posix_unlock(outer,0,64);posix_range(outer,0,64,False)
            elif name=='posix_fork_close':
                peer.ask('posix-wlock 0 64');peer.ask('fork-close');posix_range(outer,0,64,True)
                peer.ask('posix-unlock 0 64');posix_unlock(outer,0,64)
            elif name=='posix_partial_unlock':
                peer.ask('posix-wlock 0 128');peer.ask('posix-unlock 64 64')
                posix_range(outer,64,64,False)
                posix_range(outer,0,64,True)
                posix_range(outer,128,64,False)
            elif name=='posix_downgrade_upgrade':
                peer.ask('posix-wlock 0 128');peer.ask('posix-rlock 0 128')
                posix_range(outer,0,128,False,shared=True);posix_unlock(outer,0,128)
                posix_range(outer,0,128,True);posix_unlock(outer,0,128)
                peer.ask('posix-unlock 0 128')
                peer.ask('posix-rlock 0 64');peer.ask('posix-wlock 0 64')
                posix_range(outer,0,64,True);peer.ask('posix-unlock 0 64')
                posix_unlock(outer,0,64);posix_range(outer,0,64,False)
            elif name=='posix_to_eof':
                peer.ask('posix-wlock 4000 0')
                posix_range(outer,100000,1,True)
                posix_range(outer,3999,2,True)
                posix_range(outer,4000,1,True)
            elif name=='posix_blocking_grant':
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,0,0)
                peer.send('posix-wlock-w 10 64');peer.pending()
                fcntl.lockf(outer,fcntl.LOCK_UN,0,0)
                row=peer.receive();assert row['rc']==0,row
                peer.ask('posix-unlock 10 64')
            elif name=='posix_signal_eintr':
                peer.ask('signal-ready');fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,0,0)
                peer.send('posix-wlock-w 0 64');peer.pending();peer.interrupt()
                row=peer.receive();assert row['rc']==-1 and row['errno']==errno.EINTR,row
                fcntl.lockf(outer,fcntl.LOCK_UN,0,0)
                peer.ask('posix-wlock 0 64');peer.ask('posix-unlock 0 64')
            elif name=='posix_kill_release':
                peer.ask('posix-wlock 100 64');posix_range(outer,100,64,True)
                peer.kill();posix_unlock(outer,100,64);posix_range(outer,100,64,False)
            elif name=='posix_whence_cur_end':
                peer.ask('posix-wlock-end -8 8')
                posix_range(outer,8184,8,True)
                posix_range(outer,8176,8,False);posix_unlock(outer,8176,8)
                peer.ask('posix-wlock-cur 200 16')
                posix_range(outer,200,16,True)
                posix_range(outer,216,16,False)
            elif name=='posix_getlk_reports':
                peer.ask('posix-wlock 100 64')
                other=stack.enter_context(Peer(mode,path));other.ask('open')
                row=other.ask('posix-getlk 1 0 4096')
                assert row['type']==fcntl.F_WRLCK and row['start']==100 and row['len']==64,row
                if mode=='native':assert row['pid']==peer.proc.pid,row
                else:assert row['pid']>=0,row
                row=other.ask('posix-getlk 1 1000 64');assert row['type']==fcntl.F_UNLCK,row
            elif name=='posix_rename_unlink':
                peer.ask('posix-wlock 0 64')
                moved=path.with_suffix('.moved');path.rename(moved)
                posix_range(outer,0,64,True)
                moved.unlink();posix_range(outer,0,64,True)
                peer.ask('close')
                posix_unlock(outer,0,64);posix_range(outer,0,64,False)
                path.write_bytes(b'B'*4096);os.chmod(path,0o666)
                outer2=stack.enter_context(path.open('r+b',buffering=0))
                posix_range(outer2,0,64,False)
            elif name=='cancel_release_races':
                rows=[];report['cancel_races'][mode]=rows
                # None holds the outer lock until EINTR is observed. The other
                # offsets exercise grant/cancel races around the 2 ms timer.
                delays=[None,0,.001,.002,.004]
                for iteration in range(a.cancel_race_iterations):
                    fcntl.flock(outer,fcntl.LOCK_EX)
                    peer.send('timed-wait 2000')
                    armed=peer.receive();assert armed.get('armed'),armed
                    delay=delays[iteration%len(delays)]
                    if delay is not None:
                        if delay:time.sleep(delay)
                        fcntl.flock(outer,fcntl.LOCK_UN)
                    row=peer.receive()
                    assert row.get('op')=='timed-wait',row
                    rows.append({'iteration':iteration,'unlock_delay_seconds':delay,**row})
                    if delay is None:
                        assert row['rc']==-1 and row['errno']==errno.EINTR,row
                        fcntl.flock(outer,fcntl.LOCK_UN)
                    assert row['rc']==0 or (row['rc']==-1 and row['errno']==errno.EINTR),row
                    if row['rc']==0:
                        outer_lock(outer,True);peer.ask('unlock')
                    # On EINTR do NOT send unlock: that would mask a leaked
                    # successful acquisition whose reply lost the race.
                    outer_lock(outer,False)
                    assert peer.ask('read')['byte']==65
                assert any(r['rc']==0 for r in rows),'lock-grant race outcome not covered'
                assert any(r['rc']==-1 for r in rows),'lock-cancel race outcome not covered'
    def suite(mode):
        for name in names:
            try:case(mode,name);report['checks'][mode][name]='pass'
            except BaseException as e:report['checks'][mode][name]='fail';report['errors'][mode+'/'+name]=f'{type(e).__name__}: {e}'
            print(mode+' '+name+' '+report['checks'][mode][name],flush=True)
    def bench(mode,phase):
        path=share/(mode+'-bench-'+phase);path.write_bytes(b'')
        with Peer(mode,path) as peer:
            peer.ask('open')
            for sample in range(3):report['samples'].append({'mode':mode,'phase':phase,'sample':sample,**peer.ask('bench-lock 1000')})
    try:
        if not a.benchmark_only: suite('native')
        if not a.functional_only:bench('native','before')
        run(base+['start','--kernel',str(a.kernel.resolve()),'--disk',str(a.disk.resolve()),'--dns','1.1.1.1',
                  '--publish-ports','--share',str(share),'--share-cache','never','--virtiofsd',str(a.virtiofsd)]
                  +(['--share-profile'] if a.profile else [])+(['--share-inline-flock'] if a.inline_flock else [])
                  +(['--share-posix-locks'] if a.posix_locks else []),timeout=180)
        archive=out/(a.name+'-rootfs.tar');binary=(work/'share-probe').read_bytes()
        with tarfile.open(archive,'w') as tar:
            info=tarfile.TarInfo('share-probe');info.mode=0o755;info.size=len(binary);tar.addfile(info,io.BytesIO(binary))
        report['image_id']=cli('import',str(archive),image).strip();report['server']=json.loads(cli('version','--format','{{json .Server}}'))
        if not a.benchmark_only: suite('docker')
        if not a.functional_only:bench('docker','first');bench('docker','second')
        run(base+['stop'],timeout=70)
        if not a.functional_only:bench('native','after')
    except BaseException as e:report['error']=f'{type(e).__name__}: {e}';print(report['error'],flush=True)
    finally:
        if harddind.live(harddind.read_state(instance)):
            try:run(base+['stop'],timeout=70)
            except BaseException as e:report['cleanup_error']=str(e)
        report['runtime']=harddind.read_state(instance)
        report['host_after']=host_state()
        report['inputs_unchanged']=all(pathlib.Path(path).is_file() and harddind.sha(pathlib.Path(path))==sha
                                       for path,sha in report['sha256'].items())
        if a.inline_flock:
            stats=[]
            for file in instance.glob('harddind0-*.log'):
                for match in re.finditer(r'HARDDIND_INLINE_FLOCK completed=(\d+) queued=(\d+) busy_fallback=(\d+)',file.read_text()):
                    stats.append(dict(zip(('completed','queued','busy_fallback'),map(int,match.groups()))))
            report['inline_stats']=stats
            if len(stats)!=1 or stats[0]['completed']==0 or (not a.functional_only and sum(stats[0].values())<12000):
                report['error']='missing inline execution evidence'
        clean=(report['runtime'].get('phase')=='stopped' and not report['runtime'].get('forced_shutdown',True)
               and report['runtime'].get('guest_cleanup',{}).get('clean_shutdown',False)
               and report['runtime'].get('network_cleanup',{}).get('clean_shutdown',False)
               and len(report['runtime'].get('filesystem_cleanup',[]))==1
               and all(x.get('clean_shutdown') for x in report['runtime']['filesystem_cleanup'])
               and all(x['clean_shutdown'] and not x['forced'] for x in report['peers']))
        report['benchmark_complete']=(clean and report['inputs_unchanged'] and not report.get('error')
            and len(report['samples'])==12 and all(x['rc']==0 and x['iterations']==1000 for x in report['samples'])
            and all(sum(x['mode']==m for x in report['samples'])==6 for m in ('native','docker')))

        report['median_seconds']={mode:statistics.median(x['seconds'] for x in report['samples'] if x['mode']==mode)
                                  for mode in ('native','docker') if any(x['mode']==mode for x in report['samples'])}
        report['all_checks_passed']=all(v=='pass' for group in report['checks'].values() for v in group.values())
        report['functional_complete']=(clean and report['inputs_unchanged'] and not report.get('error') and not a.benchmark_only)
        result.write_text(json.dumps(report,indent=2)+'\n');log.close()
    print(json.dumps({'result':str(result),'checks':report['checks'],'median_seconds':report['median_seconds']},indent=2))
    return 0 if (report['functional_complete'] and report['all_checks_passed'] if a.functional_only else report['benchmark_complete'] and (a.benchmark_only or report['all_checks_passed'])) else 1


if __name__=='__main__':raise SystemExit(main())
