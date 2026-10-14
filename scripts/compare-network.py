#!/usr/bin/env python3
"""Serial native/one-frame/batched link comparison using the same Docker fixture.

Each guest uses its own persistent COW; results include link calls and scheduler
CPU time. Successful measurements do not mean native performance acceptance.
"""
import argparse
import io
import json
import os
import pathlib
import shlex
import socket
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import harddind
from guest_cleanup import group_members


def free_port(udp=False):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def wait_tcp(port):
    until = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1): return
        except OSError:
            if time.monotonic() >= until: raise
            time.sleep(.1)


def echo(port, streaming, size=32*1024*1024):
    block = bytes(range(256)) * 256
    errors = []
    started = time.monotonic()
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.settimeout(30)
        def send():
            try:
                for _ in range(size // len(block)): s.sendall(block)
                s.shutdown(socket.SHUT_WR)
            except BaseException as error: errors.append(str(error))
        if streaming:
            sender = threading.Thread(target=send, daemon=True); sender.start()
        for _ in range(size // len(block)):
            if not streaming: s.sendall(block)
            data = bytearray()
            while len(data) < len(block):
                part = s.recv(len(block) - len(data))
                if not part: raise RuntimeError('early EOF')
                data.extend(part)
            if data != block: raise RuntimeError('corrupt TCP payload')
        if streaming:
            sender.join(timeout=5)
            if sender.is_alive() or errors: raise RuntimeError(f'sender failed: {errors}')
            if s.recv(1) != b'': raise RuntimeError('missing TCP EOF')
    seconds = time.monotonic() - started
    return {'bytes_each_direction': size, 'seconds': seconds, 'mbps_each_direction': size*8/seconds/1e6}


def cpu_snapshot(state):
    # Read outside timed region. Account for all threads in every private guest
    # process; retain identities so short-lived or missing tasks are visible.
    ids, zombies = group_members(state['guest_pid'])
    result = {}
    for kind, pids in [('helper', [state['network_pid']]), ('guest', ids)]:
        rows = {}
        for pid in pids:
            try:
                for path in pathlib.Path(f'/proc/{pid}/task').glob('*/schedstat'):
                    ticks = harddind.start_time(int(path.parent.name))
                    rows[f'{path.parent.name}:{ticks}'] = int(path.read_text().split()[0])
            except FileNotFoundError: pass
        result[kind] = rows
    result['guest_zombies'] = zombies
    return result


def cpu_delta(before, after):
    result = {}
    for kind in ('helper', 'guest'):
        common = before[kind].keys() & after[kind].keys()
        result[kind] = {'cpu_seconds': sum(after[kind][k]-before[kind][k] for k in common)/1e9,
                        'tasks': len(common), 'new_tasks': sorted(after[kind].keys()-before[kind].keys()),
                        'lost_tasks': sorted(before[kind].keys()-after[kind].keys())}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path); p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True); p.add_argument('--name', required=True)
    p.add_argument('--dns', default='1.1.1.1')
    a=p.parse_args()
    if len(a.name)>24 or not a.name or not all(c.isalnum() or c in '-_' for c in a.name): p.error('invalid name')
    work=a.work.resolve(); repo=pathlib.Path(__file__).resolve().parents[1]; out=work/'results'
    out.mkdir(exist_ok=True); result=out/(a.name+'.json'); logfile=out/(a.name+'.log')
    names=[f'{a.name}-{i}' for i in range(4)]
    if result.exists() or logfile.exists() or any((work/'instances'/n).exists() for n in names): p.error('evidence already exists')
    os.sched_setaffinity(0, [0,1,2,3]); os.environ.pop('HARDDIND_NET_TRACE_DNS', None)
    report={'kind':'network_batch_comparison','full_acceptance':False,'comparison_complete':False,'order':[1,64,64,1],
            'host_cpu_affinity':sorted(os.sched_getaffinity(0)), 'cpu_exclusive':False, 'runs':[], 'native':[],
            'sha256':{str(f):harddind.sha(f) for f in [a.kernel,a.disk,work/'network-backend',work/'network-probe',
                        repo/'src/network_backend.c',repo/'scripts/harddind.py',pathlib.Path(__file__)]}}
    # A static executable needs no base image. Import identical deterministic
    # bytes for each guest so registry availability cannot gate the comparison.
    archive=out/(a.name+'-rootfs.tar')
    if archive.exists(): p.error('rootfs archive already exists')
    program=(work/'network-probe').read_bytes()
    with tarfile.open(archive, 'w') as tar:
        entry=tarfile.TarInfo('netprobe');entry.mode=0o755;entry.size=len(program)
        tar.addfile(entry,io.BytesIO(program))
    report['rootfs_sha256']=harddind.sha(archive)
    image='harddind-network-probe:local'
    native=None; active=None; log=logfile.open('x')
    def run(args, timeout=180):
        log.write('$ '+shlex.join(args)+'\n'); log.flush()
        r=subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        text=r.stdout.decode(errors='replace'); log.write(text+f'\n[exit={r.returncode}]\n'); log.flush()
        if r.returncode: raise RuntimeError(f'command exited {r.returncode}: {shlex.join(args)}')
        return text
    def host_state():
        return {n:pathlib.Path(f).read_text() for n,f in {'load':'/proc/loadavg','cpu_stat':'/sys/fs/cgroup/cpu.stat','pressure':'/sys/fs/cgroup/cpu.pressure'}.items()}
    try:
        report['host_before']=host_state()
        port=free_port(); native=subprocess.Popen([str(work/'network-probe'),'tcp',str(port)], stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        wait_tcp(port)
        for index,batch in enumerate(report['order']):
            print(f'starting run {index} batch={batch}',flush=True)
            instance=work/'instances'/names[index]
            base=[sys.executable,str(repo/'scripts/harddind.py'),str(work),names[index]]; active=base
            row={'batch':batch,'instance':str(instance),'checks':{},'samples':[]};report['runs'].append(row)
            report['native'].append({'before_run':index,'samples':{kind:[echo(port,kind=='stream') for _ in range(3)] for kind in ['request_reply','stream']}})
            run(base+['start','--kernel',str(a.kernel.resolve()),'--disk',str(a.disk.resolve()),'--dns',a.dns,'--network-batch',str(batch)])
            state=harddind.read_state(instance); row['start']=state
            def cli(*args):return run(base+['docker',*args])
            row['image_id']=cli('import',str(archive),image).strip()
            tcp,udp=free_port(),free_port(True)
            for protocol,outer in [('tcp',tcp),('udp',udp)]:
                cli('create','--name',protocol,'-p',f'{outer}:7777/{protocol}',image,'/netprobe',protocol,'7777','0.0.0.0')
                cli('start',protocol)
                harddind.control(instance,f'add {protocol} 127.0.0.1 {outer} {outer}')
            wait_tcp(tcp);echo(tcp,False,65536)
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
                s.settimeout(5)
                for size in [1,1200,8000,60000]:
                    for i in range(3):
                        data=bytes([i+1])*size; s.sendto(data,('127.0.0.1',udp));reply,_=s.recvfrom(65536)
                        if reply!=data:raise RuntimeError('UDP corruption')
            row['checks']['udp_fragmentation']='pass'
            for kind in ['request_reply','stream']:
                for sample in range(3):
                    before=harddind.control(instance,'stats'); cb=cpu_snapshot(state)
                    data=echo(tcp,kind=='stream');ca=cpu_snapshot(state);after=harddind.control(instance,'stats')
                    delta={k:after[k]-before[k] for k in ['rx_packets','tx_packets','rx_bytes','tx_bytes','dropped_packets','rx_syscalls','tx_syscalls','poll_calls']}
                    row['samples'].append({'kind':kind,**data,'link':delta,'cpu':cpu_delta(cb,ca)})
                    if delta['dropped_packets']:raise RuntimeError('link dropped packets during comparison')
                print(f'run {index} {kind} complete',flush=True)
            row['checks']['tcp_payload_and_eof']='pass'
            for protocol,outer in [('tcp',tcp),('udp',udp)]:harddind.control(instance,f'del {protocol} 127.0.0.1 {outer} {outer}')
            try:
                with socket.create_connection(('127.0.0.1',tcp),timeout=2):pass
            except OSError:pass
            else:raise RuntimeError('deleted forward still listening')
            row['checks']['forward_delete']='pass'
            run(base+['stop'],timeout=70);row['stop']=harddind.read_state(instance);active=None
            if row['stop']['phase']!='stopped':raise RuntimeError('unclean stop')
            row['checks']['clean_shutdown']='pass'
            result.write_text(json.dumps(report,indent=2)+'\n')
        report['native'].append({'after_all':True,'samples':{kind:[echo(port,kind=='stream') for _ in range(3)] for kind in ['request_reply','stream']}})
        report['medians']={}
        for kind in ['request_reply','stream']:
            modes={'native':[s for r in report['native'] for s in r['samples'][kind]]}
            for batch in (1,64):modes[f'batch_{batch}']=[s for r in report['runs'] if r['batch']==batch for s in r['samples'] if s['kind']==kind]
            report['medians'][kind]={mode:statistics.median(s['mbps_each_direction'] for s in samples) for mode,samples in modes.items()}
        report['comparison_complete']=True
    except BaseException as error:
        report['error']=f'{type(error).__name__}: {error}';report['comparison_complete']=False
        print(report['error'],flush=True)
    finally:
        if active:
            try:run(active+['stop'],timeout=70);report['failure_cleanup']=harddind.read_state(work/'instances'/active[-1])
            except BaseException as error:report['failure_cleanup_error']=str(error)
        if native:
            native.terminate();native.wait(timeout=5);report['native_cleanup']=harddind.cleanup_group(native.pid)
        report['host_after']=host_state(); result.write_text(json.dumps(report,indent=2)+'\n');log.close()
    print(json.dumps({k:report.get(k) for k in ['comparison_complete','medians','error']},indent=2),flush=True)
    return 0 if report['comparison_complete'] else 1

if __name__=='__main__': raise SystemExit(main())
