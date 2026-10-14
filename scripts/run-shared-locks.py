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
    p.add_argument('--benchmark-only',action='store_true',help='diagnostic timings only; functional checks remain not_tested')
    p.add_argument('--profile',action='store_true',help='diagnostic virtiofs timing; not an uninstrumented performance run')
    a=p.parse_args()
    if not a.name or len(a.name)>28 or not all(c.isalnum() or c in '-_' for c in a.name):p.error('invalid name')
    work=a.work.resolve();repo=pathlib.Path(__file__).resolve().parents[1];out=work/'results';out.mkdir(exist_ok=True)
    share=work/'shares'/a.name;instance=work/'instances'/a.name;result=out/(a.name+'.json');logfile=out/(a.name+'.log')
    if any(x.exists() for x in [share,instance,result,logfile]):p.error('refusing existing state or evidence')
    a.virtiofsd=(a.virtiofsd or work/'virtiofsd').resolve()
    share.mkdir(parents=True);os.sched_setaffinity(0,[0,1,2,3]);log=logfile.open('x')
    base=[sys.executable,str(repo/'scripts/harddind.py'),str(work),a.name];image='harddind-lock-probe:local';serial=0
    names=['outer_excludes_inner','inner_excludes_outer','shared_readers','independent_opens','dup_last_close',
           'close_dup_keeps_lock','fork_last_close','close_releases','kill_releases','blocking_inner','blocking_outer',
           'signal_cancels_wait','kill_cancels_wait','six_waiters_no_starvation','rename_keeps_lock','unlink_keeps_lock',
           'downgrade','upgrade_failure_drops_shared','readonly_bind_flock','posix_outer_excludes_inner','posix_inner_excludes_outer']
    def host_state():
        return {name:pathlib.Path(path).read_text() for name,path in {
            'loadavg':'/proc/loadavg','cpu_pressure':'/sys/fs/cgroup/cpu.pressure',
            'cpu_stat':'/sys/fs/cgroup/cpu.stat','memory_events':'/sys/fs/cgroup/memory.events'}.items()}
    report={'kind':'shared_flock_lifecycle','full_acceptance':False,'instrumented':a.profile,
            'inline_flock':a.inline_flock,'benchmark_only':a.benchmark_only,'host_before':host_state(),
            'checks':{mode:dict.fromkeys(names,'not_tested') for mode in ('native','docker')},'errors':{},'samples':[],
            'peers':[],'cpu_affinity':sorted(os.sched_getaffinity(0)),'cpu_exclusive':False,
            'sha256':{str(f):harddind.sha(f) for f in [a.kernel,a.disk,a.virtiofsd,work/'share-probe',
                repo/'scripts/harddind.py',repo/'src/share_probe.c',pathlib.Path(__file__)]},
            'limitations':['native read-only baseline uses O_RDONLY, not a read-only mount',
                           'POSIX byte-range locks remain a required gap and are tested as failures',
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
        bench('native','before')
        run(base+['start','--kernel',str(a.kernel.resolve()),'--disk',str(a.disk.resolve()),'--dns','1.1.1.1',
                  '--publish-ports','--share',str(share),'--share-cache','never','--virtiofsd',str(a.virtiofsd)]
                  +(['--share-profile'] if a.profile else [])+(['--share-inline-flock'] if a.inline_flock else []),timeout=180)
        archive=out/(a.name+'-rootfs.tar');binary=(work/'share-probe').read_bytes()
        with tarfile.open(archive,'w') as tar:
            info=tarfile.TarInfo('share-probe');info.mode=0o755;info.size=len(binary);tar.addfile(info,io.BytesIO(binary))
        report['image_id']=cli('import',str(archive),image).strip();report['server']=json.loads(cli('version','--format','{{json .Server}}'))
        if not a.benchmark_only: suite('docker')
        bench('docker','first');bench('docker','second')
        run(base+['stop'],timeout=70);bench('native','after')
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
            if len(stats)!=1 or stats[0]['completed']==0 or sum(stats[0].values())<12000:
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
        result.write_text(json.dumps(report,indent=2)+'\n');log.close()
    print(json.dumps({'result':str(result),'checks':report['checks'],'median_seconds':report['median_seconds']},indent=2))
    return 0 if report['benchmark_complete'] and (a.benchmark_only or report['all_checks_passed']) else 1


if __name__=='__main__':raise SystemExit(main())
