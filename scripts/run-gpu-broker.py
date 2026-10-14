#!/usr/bin/env python3
"""Test shared-mm NVIDIA syscall execution before implementing the UML backend."""
import argparse
import collections
import datetime
import fcntl
import gzip
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
from guest_cleanup import cleanup_group


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clone_records(content):
    pending, records = {}, []
    for line in content.splitlines():
        start = re.match(r'(\d+) +\d+:\d+:\d+\.\d+ clone\(', line)
        resumed = re.match(r'(\d+) +\d+:\d+:\d+\.\d+ <\.\.\. clone resumed>', line)
        if start and '<unfinished ...>' in line:
            if start[1] in pending:
                raise ValueError('overlapping clone entries')
            pending[start[1]] = line.split('<unfinished ...>', 1)[0]
        elif resumed:
            if resumed[1] not in pending:
                raise ValueError('unmatched clone resume')
            records.append(pending.pop(resumed[1]) + line[resumed.end():])
        elif start:
            records.append(line)
    if pending:
        raise ValueError('incomplete clone trace')
    return records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--name', required=True)
    p.add_argument('--brokers', type=int, choices=[1, 2, 3, 4], default=1)
    p.add_argument('--threads', action='store_true', help='share TGID as well as mm/files; raw syscall workers only')
    p.add_argument('--modes', nargs='+', choices=['native', 'ioctl', 'mmap', 'alias'],
                   default=['native', 'ioctl', 'mmap', 'alias'])
    args = p.parse_args()
    if not args.name or len(args.name) > 40 or not all(c.isalnum() or c in '-_' for c in args.name):
        p.error('invalid evidence name')
    if len(args.modes) != len(set(args.modes)):
        p.error('duplicate modes')
    work = args.work.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    output = work / 'results' / args.name
    output.mkdir(parents=True, exist_ok=False)
    program, shim = output / 'cuda-probe', output / 'gpu-broker.so'
    inputs = [repo / 'src/cuda_probe.c', repo / 'src/gpu_broker_probe.c', pathlib.Path(__file__).resolve()]
    report = {'kind': 'outer_shared_mm_gpu_broker', 'full_acceptance': False,
              'internal_gpu_verified': False, 'diagnostic_complete': False,
              'broker_count': args.brokers,
              'thread_workers': args.threads,
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'modes': {mode: {'status': 'not_tested'} for mode in args.modes},
              'limitations': ['outer process experiment, not an implemented UML or Docker GPU backend',
                              'helper shares CLONE_FILES; UML fd translation is not tested',
                              'strace and syscall proxy change timing; no GPU performance acceptance',
                              'one CUDA process, sequential cards; fork and concurrent client lifecycle unverified',
                              'multiple brokers alternate serially; simultaneous syscalls are not tested',
                              'device alias test maps and unmaps only; it does not read or write MMIO',
                              'guest page faults, pinned pages, UVM migration and guest cgroup accounting are unverified']}
    with (work / 'kernel-build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            os.sched_setaffinity(0, [0, 1, 2, 3])
            report['cpu_affinity'] = sorted(os.sched_getaffinity(0))
            report['host_load_before'] = pathlib.Path('/proc/loadavg').read_text()
            report['driver_version'] = pathlib.Path('/proc/driver/nvidia/version').read_text()
            report['strace_version'] = subprocess.check_output(['strace', '-V'], text=True).splitlines()[0]
            report['compiler'] = subprocess.check_output(['gcc', '--version'], text=True).splitlines()[0]
            report['sha256'] = {str(path): sha(path) for path in inputs}
            with (output / 'build.log').open('x') as log:
                for command in [
                    ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', str(inputs[0]), '-ldl', '-o', str(program)],
                    ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared',
                     '-fno-stack-protector', '-fno-builtin', str(inputs[1]), '-pthread', '-o', str(shim)],
                ]:
                    log.write(json.dumps(command) + '\n'); log.flush()
                    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            inputs += [program, shim]
            report['sha256'] = {str(path): sha(path) for path in inputs}
            for mode in args.modes:
                phase = output / mode; phase.mkdir()
                trace = phase / 'syscalls.strace'
                command = ['strace', '-f', '-qq', '-yy', '-tt', '-T', '-s', '32', '-o', str(trace),
                           '-e', 'trace=openat,ioctl,mmap,munmap,close,clone,clone3,prctl,exit,exit_group']
                if mode != 'native':
                    command += ['-E', 'LD_PRELOAD=' + str(shim), '-E', 'HARDDIND_GPU_BROKER_MODE=' + mode,
                                '-E', 'HARDDIND_GPU_BROKER_COUNT=' + str(args.brokers)]
                    if args.threads:
                        command += ['-E', 'HARDDIND_GPU_BROKER_THREADS=1']
                command.append(str(program))
                env = {k: v for k, v in os.environ.items() if k not in
                       ('LD_PRELOAD', 'HARDDIND_GPU_BROKER_MODE', 'HARDDIND_GPU_BROKER_COUNT', 'HARDDIND_GPU_BROKER_THREADS')}
                env['CUDA_CACHE_PATH'] = str(phase / 'cuda-cache')
                row = {'status': 'running', 'command': command}
                report['modes'][mode] = row
                print('running ' + mode, flush=True)
                with (phase / 'stdout.jsonl').open('x') as out, (phase / 'stderr.log').open('x') as err:
                    proc = subprocess.Popen(command, cwd=phase, env=env, stdout=out, stderr=err, start_new_session=True)
                    try:
                        row['exit_code'] = proc.wait(timeout=180); row['timed_out'] = False
                    except subprocess.TimeoutExpired:
                        row['timed_out'] = True
                        os.killpg(proc.pid, signal.SIGKILL)
                        row['exit_code'] = proc.wait(timeout=15)
                    finally:
                        if proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=15)
                        row['cleanup'] = cleanup_group(proc.pid)
                rows = [json.loads(line) for line in (phase / 'stdout.jsonl').read_text().splitlines()]
                row['compute_results'] = rows
                row['four_outer_gpus_verified'] = (len(rows) == 4 and {r['gpu'] for r in rows} == set(range(4))
                    and all(r.get('verified') is True and r['elements'] == 1 << 20 for r in rows))
                content = trace.read_text()
                row['trace_sha256'] = sha(trace)
                row['gpu_ioctl_pids'] = dict(collections.Counter(re.findall(
                    r'^(\d+) +\d+:\d+:\d+\.\d+ ioctl\(\d+</dev/nvidia', content, re.M)))
                row['gpu_mmap_pids'] = dict(collections.Counter(re.findall(
                    r'^(\d+) +\d+:\d+:\d+\.\d+ mmap\([^\n]*?\d+</dev/nvidia', content, re.M)))
                row['gpu_libraries'] = {}
                for name in sorted(set(re.findall(r'= \d+<([^>]*(?:libcuda|libnvidia)[^>]*)>', content))):
                    path = pathlib.Path(name)
                    row['gpu_libraries'][name] = sha(path) if path.is_file() else None
                stderr = (phase / 'stderr.log').read_text()
                route_valid = bool(row['gpu_ioctl_pids'])
                if mode != 'native':
                    starts = re.findall(r'HARDDIND_GPU_BROKER_START owner=(\d+) broker=(\d+) mode=(\w+)', stderr)
                    ends = re.findall(r'HARDDIND_GPU_BROKER_END failed=(\d+)', stderr)
                    stats = re.findall(r'HARDDIND_GPU_BROKER kind=(\d+) ioctls=(\d+) mmaps=(\d+) alias_ok=(\d+) alias_failed=(\d+)', stderr)
                    row['broker_stats'] = [dict(zip(['kind', 'ioctls', 'mmaps', 'alias_ok', 'alias_failed'], map(int, values))) for values in stats]
                    row['alias_errors'] = [dict(zip(['kind', 'errno', 'count'], map(int, values))) for values in re.findall(
                        r'HARDDIND_GPU_ALIAS_ERROR kind=(\d+) errno=(\d+) count=(\d+)', stderr)]
                    route_valid = (len(starts) == args.brokers and ends == ['0'] and len(stats) == 3
                                   and {int(s[0]) for s in stats} == {0, 1, 2})
                    if route_valid:
                        owners, brokers = {s[0] for s in starts}, {s[1] for s in starts}
                        row.update(owner_pids=sorted(map(int, owners)), broker_pids=sorted(map(int, brokers)))
                        row['worker_clone_records'] = [line for line in clone_records(content)
                            if any(re.search(r'= ' + pid + r'\b', line) for pid in brokers)]
                        route_valid = (len(owners) == 1 and not owners.intersection(brokers)
                            and len(brokers) == args.brokers and all(s[2] == mode for s in starts)
                            and set(row['gpu_ioctl_pids']) == brokers
                            and sum(s['ioctls'] for s in row['broker_stats']) == sum(row['gpu_ioctl_pids'].values()))
                        if args.threads:
                            route_valid = route_valid and len(row['worker_clone_records']) == args.brokers and all(
                                'CLONE_THREAD' in line and 'CLONE_VM' in line and 'CLONE_CHILD_CLEARTID' in line
                                for line in row['worker_clone_records'])
                        if mode in ('mmap', 'alias'):
                            route_valid = (route_valid and set(row['gpu_mmap_pids']) == brokers
                                and sum(s['mmaps'] + s['alias_ok'] + s['alias_failed'] for s in row['broker_stats'])
                                    == sum(row['gpu_mmap_pids'].values()))
                row['routing_verified'] = route_valid
                row['inputs_unchanged'] = all(sha(path) == report['sha256'][str(path)] for path in inputs)
                row['status'] = 'pass' if (row['exit_code'] == 0 and not row['timed_out']
                    and row['cleanup']['clean_shutdown'] and row['four_outer_gpus_verified']
                    and route_valid and row['inputs_unchanged']) else 'fail'
                with gzip.GzipFile(str(trace) + '.gz', 'wb', mtime=0) as gz:
                    gz.write(trace.read_bytes())
                trace.unlink()
                print(mode + ': ' + row['status'], flush=True)
                (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
                if row['status'] != 'pass':
                    break
            report['host_load_after'] = pathlib.Path('/proc/loadavg').read_text()
            report['diagnostic_complete'] = all(row['status'] == 'pass' for row in report['modes'].values())
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
        finally:
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(output / 'report.json'), 'diagnostic_complete': report['diagnostic_complete'],
                      'modes': {mode: row['status'] for mode, row in report['modes'].items()}, 'error': report.get('error')}, indent=2))
    return 0 if report['diagnostic_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
