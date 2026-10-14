#!/usr/bin/env python3
"""Compare schedulability of sync/async GPU waits, not GPU performance parity."""
import argparse,datetime,fcntl,hashlib,json,os,pathlib,re,shutil,signal,stat,subprocess,time,uuid
from guest_cleanup import cleanup_group

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work',type=pathlib.Path)
    parser.add_argument('--kernel',type=pathlib.Path,required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--kill-worker',action='store_true')
    a=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',a.name):parser.error('invalid experiment name')
    work=a.work.resolve();kernel=a.kernel.resolve();repo=pathlib.Path(__file__).resolve().parents[1]
    out=work/'results'/a.name;out.mkdir(parents=True,exist_ok=False)
    report={'kind':'gpu_async_schedulability','full_acceptance':False,'internal_cuda_verified':False,
        'diagnostic_complete':False,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'guest_cpus':1,'kill_worker':a.kill_worker,'purpose':'Single-vCPU diagnostic only; full Docker target remains multicore.',
        'phases':{},'limitations':['Artificial worker delay, not GPU workload performance.',
        'Not interruptible cancellation; worker-death behavior is tested separately.']}
    with (work/'kernel-build.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            os.sched_setaffinity(0,[0,1,2,3]);report['cpu_affinity']=sorted(os.sched_getaffinity(0))
            sources=out/'sources';sources.mkdir()
            for f in [repo/'src/gpu_async_probe.c',pathlib.Path(__file__).resolve(),
                      repo/'scripts/guest_cleanup.py',repo/'kernel/experiments/0017-um-gpu-async-wait.patch']:
                shutil.copy2(f,sources/f.name)
            shutil.copy2(work/'linux-7.2.10/.harddind-patches.json',out/'kernel-patches.json')
            binary=out/'gpu-async-probe'
            with (out/'build.log').open('x') as log:
                subprocess.run(['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread',
                    str(sources/'gpu_async_probe.c'),'-o',str(binary)],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
            base=work/'probe-root';root=out/'root'
            def copy_regular(src,dst):
                if not stat.S_ISREG(os.stat(src).st_mode):raise ValueError('special file: '+str(src))
                return shutil.copy2(src,dst)
            shutil.copytree(base,root,symlinks=True,copy_function=copy_regular,
                ignore=lambda directory,names: {'dev','proc','sys','run','tmp'} if pathlib.Path(directory)==base else set())
            for d in ['dev','proc','sys','run','tmp']:(root/d).mkdir(exist_ok=True)
            shutil.copy2(binary,root/'gpu-async-probe')
            init=root/'sbin/harddind-async-init'
            init.write_text("#!/bin/sh\nexport PATH=/bin:/sbin:/usr/bin:/usr/sbin\nmount -t proc proc /proc\nmount -t sysfs sysfs /sys\nmount -t devtmpfs devtmpfs /dev\n/gpu-async-probe /dev/harddind-nvidiactl 10\nresult=$?\necho HARDDIND_ASYNC_END status=$result\nsync\npoweroff -f\n")
            init.chmod(0o755);shutil.copy2(init,out/'guest-init')
            disk=out/'async.ext4'
            with disk.open('xb') as f:f.truncate(512*1024*1024)
            subprocess.run(['mkfs.ext4','-q','-F','-d',str(root),str(disk)],check=True,timeout=60)
            inputs=[kernel,binary,disk,*sources.iterdir()];report['sha256']={str(p):sha(p) for p in inputs}
            phases=[('worker-death',True)] if a.kill_worker else [('sync-1',False),('async-1',True),('sync-2',False),('async-2',True)]
            for name,enabled in phases:
                print('running '+name,flush=True);phase=out/name;phase.mkdir()
                cow=phase/'root.cow';log=phase/'output.log'
                command=[str(kernel),'umid=harddind-'+uuid.uuid4().hex[:12],
                    'mem=2G','ncpus=1','seccomp=on','seccomp_threads=1','seccomp_broker_cache=1','defer_ipi=1',
                    f'ubd0={cow},{disk}','root=/dev/ubda','rw','init=/sbin/harddind-async-init',
                    'con=null','con0=null,fd:1','ssl=null','panic=1','gpu_bridge=1',f'gpu_async_debug={6 if a.kill_worker else 2}']
                if enabled:command.append('gpu_async_wait=1')
                row={'command':command,'async_wait':enabled,'timed_out':False};report['phases'][name]=row
                with log.open('x') as stdout:
                    proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=stdout,stderr=subprocess.STDOUT,
                        start_new_session=True,env={**os.environ,'TMPDIR':str(work/'tmp')})
                    try:
                        if a.kill_worker:
                            deadline=time.monotonic()+60
                            while proc.poll() is None and time.monotonic()<deadline:
                                content=log.read_text(errors='replace')
                                pending=re.search(r'HARDDIND_GPU_ASYNC_SUBMIT host_pid=(\d+) operation=4 command=0xc030462b',content)
                                if pending:
                                    worker=int(pending[1])
                                    if os.getpgid(worker)!=proc.pid:raise RuntimeError('worker outside owned test process group')
                                    row['worker_death']={'pid':worker,'process_group':proc.pid,'submitted_command':'0xc030462b'}
                                    os.kill(worker,signal.SIGSTOP)
                                    time.sleep(0.2)
                                    worker_stat=pathlib.Path(f'/proc/{worker}/stat').read_text()
                                    row['worker_death']['stopped_state']=worker_stat[worker_stat.rfind(')')+2:].split()[0]
                                    os.kill(worker,signal.SIGKILL)
                                    row['worker_death']['killed']=True
                                    break
                                time.sleep(0.005)
                            if not row.get('worker_death',{}).get('killed'):raise RuntimeError('did not observe a submitted allocation')
                        row['exit_code']=proc.wait(timeout=90)
                    except subprocess.TimeoutExpired:
                        row['timed_out']=True;os.killpg(proc.pid,signal.SIGKILL);row['exit_code']=proc.wait()
                    finally:
                        if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
                        row['cleanup']=cleanup_group(proc.pid);cow.unlink(missing_ok=True)
                text=log.read_text(errors='replace')
                checks=re.findall(r'^GPU_ASYNC_CHECK (\w+) (pass|fail)$',text,re.M);row['checks']=checks
                metrics=re.search(r'GPU_ASYNC_INFO iterations=(\d+) ioctl_roundtrips=(\d+) ticks=(\d+) max_tick_gap_us=(\d+) ioctl_phase_ms=(\d+)',text)
                row['metrics']=dict(zip(['iterations','ioctl_roundtrips','ticks','max_tick_gap_us','ioctl_phase_ms'],map(int,metrics.groups()))) if metrics else {}
                workers=re.findall(r'HARDDIND_GPU_WORKER guest_tgid=\d+ host_pid=(\d+)',text)
                async_workers=re.findall(r'HARDDIND_GPU_ASYNC guest_tgid=\d+ host_pid=(\d+) irq=\d+',text)
                row['workers']=workers;row['async_workers']=async_workers
                expected={'open_control':'pass','rm_alloc_free_roundtrips':'pass','scheduler_tick_steady':'pass' if enabled else 'fail'}
                if a.kill_worker:expected['rm_alloc_free_roundtrips']='fail'
                row['expected_checks']=expected
                row['inputs_unchanged']=all(sha(p)==report['sha256'][str(p)] for p in inputs)
                row['matched_expectation']=(len(checks)==3 and dict(checks)==expected and len(workers)==1
                    and (async_workers==workers if enabled else not async_workers)
                    and ((row['metrics'].get('iterations')==0 and row['worker_death'].get('stopped_state')=='T'
                          and row['worker_death'].get('killed') and 'roundtrip_failed i=0 errno=10' in text)
                         if a.kill_worker else (row['metrics'].get('iterations')==10
                          and row['metrics'].get('ioctl_roundtrips')==20 and row['metrics'].get('ioctl_phase_ms',0)>=500))
                    and f'HARDDIND_ASYNC_END status={0 if enabled and not a.kill_worker else 1}' in text
                    and row['exit_code']==0 and not row['timed_out'] and row['cleanup']['clean_shutdown'] and row['inputs_unchanged'])
                row['status']=('expected_worker_failure' if a.kill_worker else 'pass' if enabled else 'expected_scheduler_failure') if row['matched_expectation'] else 'fail'
                (out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(name+': '+row['status'],flush=True)
                if not row['matched_expectation']:break
            report['diagnostic_complete']=len(report['phases'])==len(phases) and all(r['matched_expectation'] for r in report['phases'].values())
        except Exception as error:report['error']=f'{type(error).__name__}: {error}'
        finally:(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'report':str(out/'report.json'),'diagnostic_complete':report['diagnostic_complete'],'error':report.get('error')},indent=2))
    return 0 if report['diagnostic_complete'] else 1
if __name__=='__main__':raise SystemExit(main())
