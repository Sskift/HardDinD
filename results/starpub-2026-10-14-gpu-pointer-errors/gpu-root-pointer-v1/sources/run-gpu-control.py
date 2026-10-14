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
import stat
import subprocess
import uuid
from guest_cleanup import cleanup_group
from gpu_probe_checks import FD_CHECKS, RM_CHECKS, CTRL_CHECKS
from result_checks import memfd_backing_evidence


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
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--fd-registration', action='store_true')
    mode.add_argument('--rm-client', action='store_true')
    mode.add_argument('--rm-control', action='store_true')
    p.add_argument('--gpu-count', type=int, default=4)
    p.add_argument('--memfd-ram', action='store_true')
    p.add_argument('--memfd-stub', action='store_true')
    args = p.parse_args()
    if not 1 <= args.gpu_count <= 255:
        p.error('gpu-count must be between 1 and 255')
    fd_mode, rm_mode, ctrl_mode = args.fd_registration, args.rm_client, args.rm_control
    if ctrl_mode and args.gpu_count > 32: p.error('RM control supports at most 32 GPU IDs')
    marker = 'GPU_CTRL' if ctrl_mode else 'GPU_RM' if rm_mode else 'GPU_FD' if fd_mode else 'GPU_CONTROL'
    expected_checks = (CTRL_CHECKS | {f'gpu_{i}_details' for i in range(args.gpu_count)}) if ctrl_mode else RM_CHECKS if rm_mode else FD_CHECKS if fd_mode else CHECKS
    probe_source = 'gpu_rm_control_probe.c' if ctrl_mode else 'gpu_rm_probe.c' if rm_mode else 'gpu_fd_probe.c' if fd_mode else 'gpu_guest_probe.c'
    argument_suffix = '' if fd_mode or rm_mode or ctrl_mode else 'ctl'
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', args.name):
        p.error('invalid evidence name')
    work, kernel = args.work.resolve(), args.kernel.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    output = work / 'results' / args.name
    output.mkdir(parents=True, exist_ok=False)
    report = {'kind': 'uml_gpu_rm_control' if ctrl_mode else 'uml_gpu_rm_client' if rm_mode else 'uml_gpu_fd_registration' if fd_mode else 'uml_gpu_control_transport', 'full_acceptance': False,
              'internal_cuda_verified': False, 'diagnostic_complete': False,
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'phases': {name: {'status': 'not_tested'} for name in
                         ['native-before', 'disabled', 'guest', 'native-after']},
              'limitations': ['NVIDIA control, REGISTER_FD and optional RM root-client subset; no device mmap or CUDA',
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
            for path in [repo / 'src' / probe_source, pathlib.Path(__file__).resolve(),
                         repo / 'scripts/gpu_probe_checks.py',
                         repo / 'sources.lock.json',
                         repo / 'kernel/experiments/0012-um-gpu-control-transport.patch',
                         repo / 'kernel/experiments/0013-um-gpu-register-fd.patch',
                         repo / 'kernel/patches/0014-um-check-usercopy-read-permission.patch',
                         repo / 'kernel/patches/0015-um-opt-in-memfd-backing.patch',
                         repo / 'scripts/result_checks.py']:
                shutil.copy2(path, sources / path.name)
            shutil.copy2(work / 'linux-7.2.10/.harddind-patches.json', output / 'kernel-patches.json')
            if rm_mode or ctrl_mode:
                shutil.copy2(repo / 'kernel/experiments/0016-um-gpu-rm-client.patch', sources / '0016-um-gpu-rm-client.patch')
            if ctrl_mode:
                shutil.copy2(repo / 'kernel/experiments/0019-um-gpu-root-control.patch', sources / '0019-um-gpu-root-control.patch')
            program = output / 'gpu-guest-probe'
            with (output / 'build.log').open('x') as log:
                subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                                str(sources / probe_source), '-o', str(program)],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            root = output / 'root'
            base = work / 'probe-root'
            def copy_regular(source, destination):
                if not stat.S_ISREG(os.stat(source).st_mode):
                    raise ValueError('refusing to read a special file: ' + str(source))
                return shutil.copy2(source, destination)
            shutil.copytree(base, root, symlinks=True, copy_function=copy_regular,
                            ignore=lambda directory, names: {'dev', 'proc', 'sys', 'run', 'tmp'}
                            if pathlib.Path(directory) == base else set())
            for directory in ['dev', 'proc', 'sys', 'run', 'tmp']:
                (root / directory).mkdir(exist_ok=True)
            shutil.copy2(program, root / 'gpu-guest-probe')
            init = root / 'sbin/harddind-gpu-control-init'
            argument = str(args.gpu_count) if fd_mode or rm_mode or ctrl_mode else version
            report['gpu_count'] = args.gpu_count if fd_mode or rm_mode or ctrl_mode else None
            init.write_text('''#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
if [ -e /dev/harddind-nvidiactl ]; then
    /gpu-guest-probe /dev/harddind-nvidia''' + argument_suffix + ' ' + argument + '''
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
                command = [str(program), '/dev/nvidia' + argument_suffix, argument]
                if name in ('guest', 'disabled'):
                    command = [str(kernel), 'umid=harddind-' + uuid.uuid4().hex[:12],
                               'mem=2G', 'ncpus=4', 'seccomp=on', 'seccomp_threads=1',
                               'seccomp_broker_cache=1', 'defer_ipi=1',
                               f'ubd0={cow},{disk}', 'root=/dev/ubda', 'rw',
                               'init=/sbin/harddind-gpu-control-init', 'con=null',
                               'con0=null,fd:1', 'ssl=null', 'panic=1']
                    if args.memfd_ram: command.append('memfd_ram=1')
                    if args.memfd_stub: command.append('memfd_stub=1')
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
                checks = re.findall(r'^' + marker + r'_CHECK (\w+) (pass|fail)$', content, re.M)
                row['checks'] = checks
                row['worker_pids'] = re.findall(r'HARDDIND_GPU_WORKER guest_tgid=\d+ host_pid=(\d+)', content)
                trace_content = trace.read_text(errors='replace')
                ioctl_lines = [line for line in trace_content.splitlines()
                               if re.search(r'ioctl\(\d+</dev/nvidia(?:ctl|\d+)(?:<|>)', line)]
                row['device_ioctl_pids'] = sorted({line.split()[0] for line in ioctl_lines})
                row['device_ioctl_count'] = len(ioctl_lines)
                row['trace_sha256'] = sha(trace)
                row['inputs_unchanged'] = all(sha(path) == report['sha256'][str(path)] for path in inputs)
                valid = (len(checks) == len(expected_checks) and {n for n, status in checks if status == 'pass'} == expected_checks
                         and marker + '_END status=0 internal_cuda_verified=0' in content)
                if name == 'disabled':
                    valid = ('GPU_CONTROL_DISABLED status=0' in content
                             and 'HARDDIND_GPU_CONTROL_END status=0' in content
                             and not checks and not row['worker_pids'] and not ioctl_lines)
                elif name == 'guest':
                    valid = (valid and 'HARDDIND_GPU_CONTROL_END status=0' in content
                             and len(row['worker_pids']) == 5
                             and len(set(row['worker_pids'])) == 5
                             and set(row['device_ioctl_pids']) == set(row['worker_pids'])
                             and row['device_ioctl_count'] == ((91 if name != 'guest' else 89) + 3 * args.gpu_count if ctrl_mode else 201 if rm_mode else 36 + args.gpu_count if fd_mode else 153))
                else:
                    valid = valid and row['device_ioctl_count'] == ((91 if name != 'guest' else 89) + 3 * args.gpu_count if ctrl_mode else 205 if rm_mode else 37 + args.gpu_count if fd_mode else 154)
                if name in ('guest', 'disabled'):
                    row['memory_backing'] = memfd_backing_evidence(content, args.memfd_ram, args.memfd_stub)
                    if args.memfd_ram or args.memfd_stub:
                        valid = valid and row['memory_backing']['verified']
                if ctrl_mode and name != 'disabled':
                    row['control_results'] = [json.loads(x) for x in re.findall(r'^GPU_CTRL_RESULT (.+)$', content, re.M)]
                    row['device_results'] = [json.loads(x) for x in re.findall(r'^GPU_CTRL_DEVICE (.+)$', content, re.M)]
                    valid = valid and len(row['control_results']) == 13 and len(row['device_results']) == args.gpu_count
                    if name != 'native-before':
                        baseline = report['phases']['native-before']
                        row['native_contract_matches'] = (
                            row['control_results'] == baseline.get('control_results')
                            and row['device_results'] == baseline.get('device_results'))
                        valid = valid and row['native_contract_matches']
                row['status'] = 'pass' if (valid and row['exit_code'] == 0 and not row['timed_out']
                                          and row['inputs_unchanged'] and row['cleanup']['clean_shutdown']) else 'fail'
                with gzip.GzipFile(str(trace) + '.gz', 'wb', mtime=0) as gz:
                    gz.write(trace.read_bytes())
                trace.unlink()
                print(name + ': ' + row['status'], flush=True)
                (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
                if row['status'] != 'pass' and not rm_mode and not ctrl_mode:
                    break
            report['diagnostic_finished'] = all(row['status'] in ('pass', 'fail') for row in report['phases'].values())
            report['diagnostic_complete'] = all(row['status'] == 'pass' for row in report['phases'].values())
        except (Exception, KeyboardInterrupt) as error:
            report['error'] = f'{type(error).__name__}: {error}'
        finally:
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(output / 'report.json'),
                      'diagnostic_complete': report['diagnostic_complete'], 'error': report.get('error')}, indent=2))
    return 0 if report['diagnostic_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
