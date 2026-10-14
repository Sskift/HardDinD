#!/usr/bin/env python3
"""Validate opt-in UML GPU control transport; never count this as CUDA acceptance."""
import argparse
import datetime
import fcntl
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import uuid
from guest_cleanup import cleanup_group


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


CHECKS = {'open', 'real_driver_version', 'cross_page_user_pointer', 'invalid_user_pointer',
          'driver_rejects_bad_size', 'success_after_driver_error', 'dup', 'close_original',
          'query_after_dup_close', 'four_threads_same_client', 'close_last_reference',
          'reopen_lifecycle', 'independent_process_lifecycle'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True)
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', args.name):
        p.error('invalid evidence name')
    work, kernel = args.work.resolve(), args.kernel.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    output = work / 'results' / args.name
    output.mkdir(parents=True, exist_ok=False)
    report = {'kind': 'uml_gpu_control_transport', 'full_acceptance': False,
              'internal_cuda_verified': False, 'diagnostic_complete': False,
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'phases': {name: {'status': 'not_tested'} for name in
                         ['native-before', 'disabled', 'guest', 'native-after']},
              'limitations': ['flat NVIDIA control ioctls only; no device mmap or CUDA',
                              'one GPU identity per guest mm; cross-group fd use fails EXDEV',
                              'device waits disable one guest CPU interrupts; blocking GPU commands unavailable',
                              'traced functional test, not a performance comparison',
                              'UML stub internal isolation is still incomplete']}
    with (work / 'kernel-build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            os.sched_setaffinity(0, [0, 1, 2, 3])
            report['cpu_affinity'] = sorted(os.sched_getaffinity(0))
            driver = pathlib.Path('/proc/driver/nvidia/version').read_text()
            version = re.search(r'Kernel Module\s+(\d+(?:\.\d+)+)', driver)[1]
            report['driver_version'] = driver
            sources = output / 'sources'; sources.mkdir()
            for path in [repo / 'src/gpu_guest_probe.c', pathlib.Path(__file__).resolve(),
                         repo / 'kernel/experiments/0012-um-gpu-control-transport.patch']:
                shutil.copy2(path, sources / path.name)
            shutil.copy2(work / 'linux-7.2.10/.harddind-patches.json', output / 'kernel-patches.json')
            program = output / 'gpu-guest-probe'
            with (output / 'build.log').open('x') as log:
                subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                                str(sources / 'gpu_guest_probe.c'), '-o', str(program)],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            root = output / 'root'
            shutil.copytree(work / 'probe-root', root, symlinks=True)
            shutil.copy2(program, root / 'gpu-guest-probe')
            init = root / 'sbin/harddind-gpu-control-init'
            init.write_text('''#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
if grep -q 'gpu_bridge=1' /proc/cmdline; then
    /gpu-guest-probe /dev/harddind-nvidiactl ''' + version + '''
    result=$?
else
    result=0
    if [ -e /dev/harddind-nvidiactl ]; then result=1; fi
    echo "GPU_CONTROL_DISABLED status=$result"
fi
echo "HARDDIND_GPU_CONTROL_END status=$result"
sync
poweroff -f
''')
            init.chmod(0o755)
            shutil.copy2(init, output / 'guest-init')
            disk = output / 'gpu-control.ext4'
            with disk.open('xb') as f:
                f.truncate(512 * 1024 * 1024)
            subprocess.run(['mkfs.ext4', '-q', '-F', '-d', str(root), str(disk)], check=True, timeout=60)
            inputs = [kernel, disk, program, *sources.iterdir()]
            report['sha256'] = {str(path): sha(path) for path in inputs}
            for name, row in report['phases'].items():
                print('running ' + name, flush=True)
                phase = output / name; phase.mkdir()
                trace, log = phase / 'syscalls.strace', phase / 'output.log'
                cow = output / ('guest-' + uuid.uuid4().hex + '.cow')
                command = [str(program), '/dev/nvidiactl', version]
                if name in ('guest', 'disabled'):
                    command = [str(kernel), 'umid=harddind-' + uuid.uuid4().hex[:12],
                               'mem=2G', 'ncpus=4', 'seccomp=on', 'seccomp_threads=1',
                               'seccomp_broker_cache=1', 'defer_ipi=1',
                               f'ubd0={cow},{disk}', 'root=/dev/ubda', 'rw',
                               'init=/sbin/harddind-gpu-control-init', 'con=null',
                               'con0=null,fd:1', 'ssl=null', 'panic=1']
                    if name == 'guest':
                        command.append('gpu_bridge=1')
                traced = ['strace', '-f', '-qq', '-yy', '-tt', '-T', '-s', '80',
                          '-e', 'trace=ioctl,dup,close,openat,recvmsg,clone,exit,exit_group',
                          '-o', str(trace), *command]
                row['command'] = traced
                row['timed_out'] = False
                with log.open('x') as stdout:
                    proc = subprocess.Popen(traced, stdin=subprocess.DEVNULL, stdout=stdout,
                                            stderr=subprocess.STDOUT, start_new_session=True,
                                            env={**os.environ, 'TMPDIR': str(work / 'tmp')})
                    try:
                        row['exit_code'] = proc.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        row['timed_out'] = True
                        os.killpg(proc.pid, signal.SIGKILL)
                        row['exit_code'] = proc.wait()
                    finally:
                        if proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
                        row['cleanup'] = cleanup_group(proc.pid)
                        cow.unlink(missing_ok=True)
                content = log.read_text(errors='replace')
                checks = re.findall(r'^GPU_CONTROL_CHECK (\w+) (pass|fail)$', content, re.M)
                row['checks'] = checks
                row['worker_pids'] = re.findall(r'HARDDIND_GPU_WORKER guest_tgid=\d+ host_pid=(\d+)', content)
                trace_content = trace.read_text(errors='replace')
                ioctl_lines = [line for line in trace_content.splitlines()
                               if re.search(r'ioctl\(\d+</dev/nvidiactl(?:<|>)', line)]
                row['device_ioctl_pids'] = sorted({line.split()[0] for line in ioctl_lines})
                row['device_ioctl_count'] = len(ioctl_lines)
                row['trace_sha256'] = sha(trace)
                row['inputs_unchanged'] = all(sha(path) == report['sha256'][str(path)] for path in inputs)
                valid = (len(checks) == len(CHECKS) and {n for n, status in checks if status == 'pass'} == CHECKS
                         and 'GPU_CONTROL_END status=0 internal_cuda_verified=0' in content)
                if name == 'disabled':
                    valid = ('GPU_CONTROL_DISABLED status=0' in content
                             and 'HARDDIND_GPU_CONTROL_END status=0' in content
                             and not checks and not row['worker_pids'] and not ioctl_lines)
                elif name == 'guest':
                    valid = (valid and 'HARDDIND_GPU_CONTROL_END status=0' in content
                             and len(row['worker_pids']) == 5
                             and len(set(row['worker_pids'])) == 5
                             and set(row['device_ioctl_pids']) == set(row['worker_pids'])
                             and row['device_ioctl_count'] == 153)
                else:
                    valid = valid and row['device_ioctl_count'] == 154
                row['status'] = 'pass' if (valid and row['exit_code'] == 0 and not row['timed_out']
                                          and row['inputs_unchanged'] and row['cleanup']['clean_shutdown']) else 'fail'
                with gzip.GzipFile(str(trace) + '.gz', 'wb', mtime=0) as gz:
                    gz.write(trace.read_bytes())
                trace.unlink()
                print(name + ': ' + row['status'], flush=True)
                (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
                if row['status'] != 'pass':
                    break
            report['diagnostic_complete'] = all(row['status'] == 'pass' for row in report['phases'].values())
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
        finally:
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(output / 'report.json'),
                      'diagnostic_complete': report['diagnostic_complete'], 'error': report.get('error')}, indent=2))
    return 0 if report['diagnostic_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
