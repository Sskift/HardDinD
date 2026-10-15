#!/usr/bin/env python3
"""Record the outer NVIDIA syscall path for backend implementation, not GPU acceptance."""
import argparse
import collections
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import time
from guest_cleanup import cleanup_group


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--name', required=True)
    p.add_argument('--timeout', type=int, default=180)
    args = p.parse_args()
    if not args.name or not all(c.isalnum() or c in '-_' for c in args.name):
        p.error('invalid evidence name')
    if not 1 <= args.timeout <= 600:
        p.error('timeout must be 1..600 seconds')
    work = args.work.resolve()
    output = work / 'results' / args.name
    output.mkdir(parents=True, exist_ok=False)
    source = pathlib.Path(__file__).resolve().parents[1] / 'src/cuda_probe.c'
    binary = output / 'cuda-probe'
    trace = output / 'syscalls.strace'
    report = {'kind': 'outer_gpu_abi_trace', 'full_acceptance': False, 'internal_gpu_verified': False,
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'limitations': ['outer execution only; no guest NVIDIA backend is implemented here',
                              'strace changes scheduling and timing; CUDA timings are diagnostic only',
                              'one process, sequential devices; no concurrency or lifecycle acceptance',
                              'ioctl entry counts do not decode nested pointer or object semantics']}
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    # Keep this diagnostic from overlapping this repository's guest measurements/builds.
    with (work / 'kernel-build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            os.sched_setaffinity(0, [0, 1, 2, 3])
            report['cpu_affinity'] = sorted(os.sched_getaffinity(0))
            report['cpu_exclusive'] = False
            report['host_load_before'] = pathlib.Path('/proc/loadavg').read_text()
            report['driver_version'] = pathlib.Path('/proc/driver/nvidia/version').read_text()
            report['strace_version'] = subprocess.check_output(['strace', '-V'], text=True).splitlines()[0]
            command = ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', str(source), '-ldl', '-o', str(binary)]
            with (output / 'build.log').open('x') as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            report['sha256'] = {'source': sha(source), 'binary': sha(binary), 'runner': sha(pathlib.Path(__file__))}
            command = ['strace', '-f', '-qq', '-yy', '-tt', '-T', '-s', '32', '-o', str(trace),
                       '-e', 'trace=openat,ioctl,mmap,munmap,mprotect,close,clone,clone3,poll,ppoll,eventfd2',
                       str(binary)]
            report['command'] = command
            started = time.monotonic()
            with (output / 'stdout.jsonl').open('x') as log, (output / 'stderr.log').open('x') as err:
                proc = subprocess.Popen(command, stdout=log, stderr=err, cwd=output,
                                        env={**os.environ, 'CUDA_CACHE_PATH': str(output / 'cuda-cache')},
                                        start_new_session=True)
                try:
                    report['exit_code'] = proc.wait(timeout=args.timeout)
                    report['timed_out'] = False
                except subprocess.TimeoutExpired:
                    report['timed_out'] = True
                    os.killpg(proc.pid, signal.SIGKILL)
                    report['exit_code'] = proc.wait(timeout=15)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=15)
                    report['cleanup'] = cleanup_group(proc.pid)
            report['wall_seconds'] = time.monotonic() - started
            report['host_load_after'] = pathlib.Path('/proc/loadavg').read_text()
            report['compute_results'] = [json.loads(line) for line in (output / 'stdout.jsonl').read_text().splitlines()]
            rows = report['compute_results']
            report['four_outer_gpus_verified'] = (len(rows) == 4 and {r['gpu'] for r in rows} == set(range(4))
                and all(r.get('verified') is True and r['elements'] == 1 << 20 for r in rows))
            content = trace.read_text()
            report['trace_sha256'] = sha(trace)
            entries = collections.Counter(re.findall(r'^\d+ +\d+:\d+:\d+\.\d+ (\w+)\(', content, re.M))
            report['syscall_entries'] = dict(entries)
            report['nvidia_ioctl_entries'] = len(re.findall(r' ioctl\(\d+</dev/nvidia', content))
            report['loaded_gpu_libraries'] = {}
            for name in sorted(set(re.findall(r'= \d+<([^>]*(?:libcuda|libnvidia)[^>]*)>', content))):
                path = pathlib.Path(name)
                report['loaded_gpu_libraries'][name] = sha(path) if path.is_file() else None
            report['binary_unchanged'] = report['sha256']['binary'] == sha(binary)
            report['diagnostic_complete'] = (report['exit_code'] == 0 and not report['timed_out']
                and report['cleanup']['clean_shutdown'] and report['four_outer_gpus_verified']
                and report['binary_unchanged'] and report['nvidia_ioctl_entries'] > 0)
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
            report['diagnostic_complete'] = False
        finally:
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report.get('diagnostic_complete') else 1


if __name__ == '__main__':
    raise SystemExit(main())
