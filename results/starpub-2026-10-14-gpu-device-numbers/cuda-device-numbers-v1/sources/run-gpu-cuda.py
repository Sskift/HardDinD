#!/usr/bin/env python3
"""Run the real CUDA compute probe inside Docker and retain failing ABI calls."""
import argparse
import datetime
import fcntl
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import harddind
from guest_cleanup import cleanup_group


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('work',type=pathlib.Path)
    p.add_argument('--kernel',type=pathlib.Path,required=True)
    p.add_argument('--disk',type=pathlib.Path,required=True)
    p.add_argument('--name',required=True)
    p.add_argument('--gpu-count',type=int,default=4)
    a=p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,28}',a.name) or not 1<=a.gpu_count<=32:
        p.error('invalid name or GPU count')
    work=a.work.resolve();repo=pathlib.Path(__file__).resolve().parents[1]
    out=work/'results'/a.name;instance=work/'instances'/a.name
    if instance.exists():p.error('refusing existing instance')
    out.mkdir(parents=True,exist_ok=False)
    inputs=[a.kernel.resolve(),a.disk.resolve(),repo/'src/cuda_probe.c',repo/'src/cuda_ioctl_trace.c',
            pathlib.Path(__file__).resolve(),repo/'scripts/harddind.py',repo/'scripts/guest_cleanup.py']
    report={'kind':'nested_cuda_compute_diagnostic','full_acceptance':False,
            'internal_cuda_verified':False,'diagnostic_complete':False,
            'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'sha256':{str(f):harddind.sha(f) for f in inputs},'gpu_count':a.gpu_count,
            'phases':{},'limitations':['manual device aliases; not Docker --gpus integration',
            'one process, sequential cards; concurrency and memory lifecycle stress remain',
            'ioctl tracing changes timing; not GPU performance acceptance']}
    (out/'sources').mkdir()
    for f in inputs[2:]:shutil.copy2(f,out/'sources'/f.name)
    base=[sys.executable,str(repo/'scripts/harddind.py'),str(work),a.name]
    container='cuda-compute';image='harddind-cuda:local'
    report['driver_version']=pathlib.Path('/proc/driver/nvidia/version').read_text()
    os.sched_setaffinity(0,[0,1,2,3]);report['cpu_affinity']=sorted(os.sched_getaffinity(0))
    with (out/'commands.log').open('x') as commands:
        def run(label,cmd,*,stdin=None,env=None,timeout=180):
            commands.write('$ '+shlex.join(cmd)+'\n');commands.flush()
            with (out/(label+'.stdout')).open('xb') as stdout,(out/(label+'.stderr')).open('xb') as stderr:
                proc=subprocess.Popen(cmd,stdin=stdin or subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                                      env=env,start_new_session=True)
                timed_out=False
                try:rc=proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out=True;os.killpg(proc.pid,signal.SIGKILL);rc=proc.wait(timeout=15)
                finally:
                    if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=15)
                    cleanup=cleanup_group(proc.pid)
            commands.write(f'[{label} exit={rc} timeout={timed_out}]\n');commands.flush()
            return {'exit_code':rc,'timed_out':timed_out,'cleanup':cleanup,'command':cmd}
        def require(label,cmd,**kwargs):
            row=run(label,cmd,**kwargs)
            if row['exit_code'] or row['timed_out'] or not row['cleanup']['clean_shutdown']:
                raise RuntimeError(label+' failed')
            return row
        def compute(label,cmd,env=None):
            row=run(label,cmd,env=env)
            text=(out/(label+'.stdout')).read_text(errors='replace')
            rows=[json.loads(line) for line in text.splitlines() if line.startswith('{"gpu":')]
            error=(out/(label+'.stderr')).read_text(errors='replace')
            row['compute_results']=rows
            row['ioctl_records']=[line for line in error.splitlines() if line.startswith('CUDA_IOCTL ')]
            row['errors']=[line for line in error.splitlines() if not line.startswith('CUDA_IOCTL ')]
            row['all_gpus_verified']=(row['exit_code']==0 and not row['timed_out']
                and row['cleanup']['clean_shutdown'] and len(rows)==a.gpu_count
                and {r['gpu'] for r in rows}==set(range(a.gpu_count))
                and all(r.get('verified') is True and r.get('elements')==1<<20 for r in rows))
            report['phases'][label]=row
            return row
        try:
            program=out/'cuda-probe';logger=out/'cuda-ioctl-trace.so'
            require('build-probe',['gcc','-O2','-Wall','-Wextra','-Werror',str(repo/'src/cuda_probe.c'),'-ldl','-o',str(program)])
            require('build-logger',['gcc','-O2','-Wall','-Wextra','-Werror','-shared','-fPIC',str(repo/'src/cuda_ioctl_trace.c'),'-o',str(logger)])
            inputs += [program,logger]
            report['sha256'].update({str(f):harddind.sha(f) for f in [program,logger]})
            listing=subprocess.check_output(['ldconfig','-p'],text=True)
            libs=[]
            for name in ['libcuda.so.1','libnvidia-ptxjitcompiler.so.1']:
                candidates=[line.split('=>',1)[1].strip() for line in listing.splitlines()
                    if line.strip().startswith(name+' ') and 'x86-64' in line and '=>' in line]
                if not candidates:raise RuntimeError('missing '+name)
                libs.append(pathlib.Path(candidates[0]))
            files=set(libs+[pathlib.Path('/bin/true'),pathlib.Path('/usr/bin/strace')])
            for f in [program,logger,pathlib.Path('/bin/true'),pathlib.Path('/usr/bin/strace')]+libs:
                listing=subprocess.check_output(['ldd',str(f)],text=True)
                if 'not found' in listing:raise RuntimeError('unresolved dependency for '+str(f))
                files.update(pathlib.Path(v) for v in re.findall(r'(/[^\s()]+)',listing))
            report['library_sha256']={str(f):harddind.sha(f) for f in sorted(files)}
            root=out/'root';root.mkdir()
            for d in ['dev','proc','sys','tmp','etc','run']:(root/d).mkdir()
            shutil.copy2(program,root/'cuda-probe');shutil.copy2(logger,root/'cuda-ioctl-trace.so')
            for f in sorted(files):
                dst=root/f.relative_to('/');dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(f,dst,follow_symlinks=True)
            with tarfile.open(out/'rootfs.tar','w') as archive:
                for f in sorted(root.iterdir()):archive.add(f,arcname=f.name)
            native_env={**os.environ,'LD_PRELOAD':str(logger),'CUDA_CACHE_PATH':str(out/'native-cache')}
            with (work/'kernel-build.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                compute('native-before',[str(program)],native_env)
            require('start',base+['start','--kernel',str(a.kernel.resolve()),'--disk',str(a.disk.resolve()),
                        '--gpu-control-experiment','--memfd-ram','--memfd-stub'])
            report['ready_state']=harddind.read_state(instance)
            with (out/'rootfs.tar').open('rb') as archive:require('import',base+['docker','import','-',image],stdin=archive)
            devices=['--device=/dev/harddind-nvidiactl:/dev/nvidiactl']
            devices += [f'--device=/dev/harddind-nvidia{i}:/dev/nvidia{i}' for i in range(a.gpu_count)]
            # Add the UVM node when the tested backend actually provides it.
            # A missing node is recorded, not replaced by an outer host device.
            probe=run('uvm-node',base+['docker','run','--rm','--device=/dev/harddind-nvidia-uvm:/dev/nvidia-uvm',image,'/bin/true'])
            probe['stderr']=(out/'uvm-node.stderr').read_text(errors='replace')
            report['uvm_node_probe']=probe
            missing=(probe['exit_code']>0 and 'error gathering device information' in probe['stderr']
                     and '/dev/harddind-nvidia-uvm' in probe['stderr'] and 'no such file' in probe['stderr'])
            if probe['timed_out'] or not probe['cleanup']['clean_shutdown'] or (probe['exit_code']!=0 and not missing):
                raise RuntimeError('UVM node probe failed without a missing device')
            report['uvm_node_available']=probe['exit_code']==0
            if report['uvm_node_available']:devices+=['--device=/dev/harddind-nvidia-uvm:/dev/nvidia-uvm']
            trace_args=['/usr/bin/strace','-f','-qq','-yy','-s','128','-o','/tmp/cuda-syscalls.strace',
                        '-e','trace=%file,ioctl,mmap,munmap,clone,clone3']
            cmd=base+['docker','run','--name',container,*devices,'--env=LD_PRELOAD=/cuda-ioctl-trace.so',
                      '--env=CUDA_CACHE_PATH=/tmp/cuda-cache',image,*trace_args,'/cuda-probe']
            compute('docker',cmd)
            require('copy-trace',base+['docker','cp',container+':/tmp/cuda-syscalls.strace',str(out/'docker.strace')])
            report['phases']['docker']['trace_sha256']=harddind.sha(out/'docker.strace')
            require('remove',base+['docker','rm',container])
            require('stop',base+['stop'],timeout=90)
            report['stopped_state']=harddind.read_state(instance)
            with (work/'kernel-build.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                compute('native-after',[str(program)],native_env)
            report['inputs_unchanged']=all(harddind.sha(f)==report['sha256'][str(f)] for f in inputs)
            report['libraries_unchanged']=all(harddind.sha(pathlib.Path(f))==h for f,h in report['library_sha256'].items())
            report['internal_cuda_verified']=report['phases']['docker']['all_gpus_verified']
        except Exception as error:report['error']=f'{type(error).__name__}: {error}'
        finally:
            try:
                state=harddind.read_state(instance)
                if harddind.live(state) and state.get('phase')=='ready':run('cleanup-container',base+['docker','rm','-f',container],timeout=30)
                run('final-stop',base+['stop'],timeout=90)
                report['final_state']=harddind.read_state(instance)
                state=report['final_state']
                report['normal_shutdown']=(state.get('phase')=='stopped' and not state.get('forced_shutdown')
                    and state.get('guest_cleanup',{}).get('clean_shutdown')
                    and state.get('network_cleanup',{}).get('clean_shutdown'))
            except Exception as error:report['cleanup_error']=str(error)
            saved=out/'instance';saved.mkdir()
            for f in instance.glob('*'):
                if f.is_file() and f.suffix in ('.json','.log'):shutil.copy2(f,saved/f.name)
            report['diagnostic_complete']=(not report.get('error') and not report.get('cleanup_error')
                and len(report['phases'])==3 and report.get('normal_shutdown')
                and report.get('inputs_unchanged') and report.get('libraries_unchanged')
                and all(v['cleanup']['clean_shutdown'] and not v['timed_out'] for v in report['phases'].values()))
            (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report.get(k) for k in ['diagnostic_complete','internal_cuda_verified','error','cleanup_error','normal_shutdown']},indent=2))
    return 0 if (report['diagnostic_complete'] and report['internal_cuda_verified']
                 and all(v['all_gpus_verified'] for v in report['phases'].values())) else 1


if __name__=='__main__':raise SystemExit(main())
