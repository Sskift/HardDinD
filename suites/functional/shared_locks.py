"""Shared lock effects ported from archive/evidence-20261015."""
import contextlib
import ctypes
import errno
import fcntl
import os
import select
import time

from suites.functional.peer import Peer as ProbePeer

NAMES = ['outer_excludes_inner', 'inner_excludes_outer', 'shared_readers', 'independent_opens', 'dup_last_close', 'close_dup_keeps_lock', 'fork_last_close', 'close_releases', 'kill_releases', 'blocking_inner', 'blocking_outer', 'signal_cancels_wait', 'kill_cancels_wait', 'six_waiters_no_starvation', 'rename_keeps_lock', 'unlink_keeps_lock', 'downgrade', 'upgrade_failure_drops_shared', 'readonly_bind_flock', 'posix_outer_excludes_inner', 'posix_inner_excludes_outer', 'posix_one_byte_zero', 'posix_non_overlapping', 'posix_readers_coexist', 'posix_independent_guest_processes', 'posix_self_multi_open', 'posix_close_dup_releases', 'posix_fork_close', 'posix_partial_unlock', 'posix_downgrade_upgrade', 'posix_to_eof', 'posix_blocking_grant', 'posix_signal_eintr', 'posix_kill_release', 'posix_whence_cur_end', 'posix_whence_before_bof', 'posix_getlk_reports', 'posix_getlk_child_exact_pid', 'posix_getlk_host_pid_zero', 'posix_deadlock_cycle', 'posix_two_file_deadlock_cycle', 'posix_exec_preserves', 'posix_exec_missing', 'posix_readonly_rights', 'posix_cancel_grant_race', 'posix_large_offsets', 'posix_rename_unlink']

def run(c, name):
    share = c.ctx.run_dir / "share" / c.id
    share.mkdir(parents=True, exist_ok=True)
    os.chmod(share, 0o755)
    def Peer(mode, path, uid=None, readonly=False):
        return ProbePeer(c, mode, path, uid=uid, readonly=readonly)
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
                row=peer.ask('posix-wlock-end -8193 1',ok=False)
                assert row['rc']==-1 and row['errno']==errno.EINVAL,row
            elif name=='posix_getlk_child_exact_pid':
                row=peer.ask('fork-locker 100 64')
                child_pid=row['child_pid']
                reported=peer.ask('posix-getlk 1 0 4096')
                assert reported['type']==fcntl.F_WRLCK and reported['start']==100 and reported['len']==64,reported
                assert reported['pid']==child_pid,('exact pid mismatch',reported,child_pid)
                peer.ask('release-child')
            elif name=='posix_getlk_host_pid_zero':
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,64,300,os.SEEK_SET)
                row=peer.ask('posix-getlk 1 256 4096')
                assert row['type']==fcntl.F_WRLCK and row['start']==300 and row['len']==64,row
                if mode=='native':
                    assert row['pid']==os.getpid(),('native host holder pid',row,os.getpid())
                else:
                    assert row['pid']==0,('cross-namespace host holder must report 0',row)
                fcntl.lockf(outer,fcntl.LOCK_UN,64,300,os.SEEK_SET)
            elif name=='posix_deadlock_cycle':
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
                peer.ask('posix-wlock 128 64')
                other.send('posix-wlock-wy 128 64');other.pending()
                peer.ask('posix-unlock 128 64')
                row=other.receive(timeout=10);assert row['rc']==0,row
                other.ask('posix-unlocky 128 64')
            elif name=='posix_exec_preserves':
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
                verifier=stack.enter_context(Peer(mode,path));verifier.ask('open')
                row=peer.ask('exec-missing 200 64',ok=False)
                assert row['rc']==-1 and row['errno']==errno.ENOENT,row
                assert row['exited'] and row['exit_code']==127 and not row['signaled'],row
                verifier.ask('posix-wlock 200 64');verifier.ask('posix-unlock 200 64')
            elif name=='posix_readonly_rights':
                peer.ask('close');peer.ask('open-read')
                row=peer.ask('posix-wlock 0 64',ok=False)
                assert row['rc']==-1 and row['errno']==errno.EBADF,row
                peer.ask('posix-rlock 0 64')
                posix_range(outer,0,64,True)
                f2=stack.enter_context(path.open('rb',buffering=0))
                try:
                    fcntl.lockf(f2,fcntl.LOCK_SH|fcntl.LOCK_NB,64,0);got=True
                except BlockingIOError:got=False
                assert got,'read locks must share'
                peer.ask('posix-unlock 0 64')
            elif name=='posix_cancel_grant_race':
                verifier=stack.enter_context(Peer(mode,path));verifier.ask('open')
                fcntl.lockf(outer,fcntl.LOCK_EX|fcntl.LOCK_NB,64,128)
                peer.send('posix-timed-wait 128 64 5000')
                armed=peer.receive(timeout=5);assert armed.get('armed') is True,armed
                row=peer.receive(timeout=10)
                assert row['rc']==-1 and row['errno']==errno.EINTR,row
                assert verifier.ask('posix-wlock 128 64',ok=False)['errno']==errno.EAGAIN
                fcntl.lockf(outer,fcntl.LOCK_UN,64,128)
                verifier.ask('posix-wlock 128 64');verifier.ask('posix-unlock 128 64')
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
    case("docker", name)
