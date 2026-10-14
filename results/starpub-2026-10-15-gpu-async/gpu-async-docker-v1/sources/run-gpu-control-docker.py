#!/usr/bin/env python3
"""Exercise the experimental NVIDIA control node inside real nested Docker."""
import argparse
import datetime
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import harddind
from gpu_probe_checks import FD_CHECKS, RM_CHECKS, CTRL_CHECKS, BUILD_CHECKS
from result_checks import memfd_backing_evidence


CHECKS = {'open', 'real_driver_version', 'cross_page_user_pointer', 'invalid_user_pointer',
          'driver_rejects_bad_size', 'success_after_driver_error', 'dup', 'close_original',
          'query_after_dup_close', 'four_threads_same_client', 'close_last_reference',
          'reopen_lifecycle', 'independent_process_lifecycle'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--probe', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--fd-registration', action='store_true')
    mode.add_argument('--rm-client', action='store_true')
    mode.add_argument('--rm-control', action='store_true')
    mode.add_argument('--build-version', action='store_true')
    p.add_argument('--native-baseline', type=pathlib.Path)
    p.add_argument('--gpu-count', type=int, default=4)
    p.add_argument('--async-wait', action='store_true')
    p.add_argument('--memfd-ram', action='store_true')
    p.add_argument('--memfd-stub', action='store_true')
    a = p.parse_args()
    if not 1 <= a.gpu_count <= 255:
        p.error('gpu-count must be between 1 and 255')
    fd_mode, rm_mode, ctrl_mode, bv_mode = a.fd_registration, a.rm_client, a.rm_control, a.build_version
    if (ctrl_mode and a.gpu_count > 32) or ((ctrl_mode or bv_mode) and not a.native_baseline):
        p.error('rm-control/build-version require --native-baseline; rm-control requires gpu-count <= 32')
    if a.native_baseline and not (ctrl_mode or bv_mode):
        p.error('--native-baseline requires --rm-control or --build-version')
    marker = 'GPU_BUILD' if bv_mode else 'GPU_CTRL' if ctrl_mode else 'GPU_RM' if rm_mode else 'GPU_FD' if fd_mode else 'GPU_CONTROL'
    expected_checks = BUILD_CHECKS if bv_mode else (CTRL_CHECKS | {f'gpu_{i}_details' for i in range(a.gpu_count)}) if ctrl_mode else RM_CHECKS if rm_mode else FD_CHECKS if fd_mode else CHECKS
    devices = ['--device=/dev/harddind-nvidiactl']
    if fd_mode or rm_mode:
        devices += [f'--device=/dev/harddind-nvidia{i}' for i in range(a.gpu_count)]
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,28}', a.name):
        p.error('invalid experiment name')
    work = a.work.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    out = work / 'results' / a.name
    instance = work / 'instances' / a.name
    if instance.exists():
        p.error('refusing existing instance')
    out.mkdir(parents=True, exist_ok=False)
    inputs = [a.kernel.resolve(), a.disk.resolve(), a.probe.resolve(),
              repo / 'scripts/harddind.py', pathlib.Path(__file__).resolve(),
              repo / 'scripts/gpu_probe_checks.py', repo / 'scripts/result_checks.py']
    if ctrl_mode or bv_mode:
        inputs.append(a.native_baseline.resolve())
    report = {'kind': 'docker_gpu_build_version' if bv_mode else 'docker_gpu_rm_control' if ctrl_mode else 'docker_gpu_rm_client' if rm_mode else 'docker_gpu_fd_registration' if fd_mode else 'docker_gpu_control_transport', 'full_acceptance': False,
              'internal_cuda_verified': False, 'diagnostic_complete': False, 'probe_results': {},
              'async_wait': a.async_wait,
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'sha256': {str(f): harddind.sha(f) for f in inputs},
              'checks': {n: 'not_tested' for n in ['no_device', 'device_control', 'nonroot_denied',
                                                 'device_readonly_denied', 'container_restart',
                                                 'configuration_mismatch_rejected',
                                                 'concurrent_client_a', 'concurrent_client_b',
                                                 'instance_restart',
                                                 'inputs_unchanged', 'normal_shutdown']}}
    if a.async_wait: report['checks']['async_wait_active'] = 'not_tested'
    if a.memfd_ram or a.memfd_stub:
        report['checks']['memory_backing'] = 'not_tested'
    sources = out / 'sources'; sources.mkdir()
    for f in inputs[3:]: shutil.copy2(f, sources / f.name)
    base = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), a.name]
    image, container = 'harddind-gpu-control:local', 'gpu-control'
    version_text = pathlib.Path('/proc/driver/nvidia/version').read_text()
    version = re.search(r'Kernel Module\s+(\d+(?:\.\d+)+)', version_text)[1]
    report['driver_version'] = version_text
    baseline = None
    if ctrl_mode or bv_mode:
        baseline_report = json.loads(a.native_baseline.read_text())
        baseline = baseline_report.get('phases', {}).get('native-before', {})
        after = baseline_report.get('phases', {}).get('native-after', {})
        recorded_probe = str(a.native_baseline.resolve().parent / 'gpu-guest-probe')
        if not (baseline_report.get('kind') == ('uml_gpu_build_version' if bv_mode else 'uml_gpu_rm_control')
                and baseline_report.get('diagnostic_complete')
                and baseline_report.get('gpu_count') == a.gpu_count
                and baseline_report.get('driver_version') == version_text
                and baseline_report.get('sha256', {}).get(recorded_probe) == harddind.sha(a.probe)
                and baseline.get('status') == after.get('status') == 'pass'
                and len(baseline.get('control_results', [])) == (24 if bv_mode else 14)
                and len(baseline.get('device_results', [])) == (0 if bv_mode else a.gpu_count)
                and baseline['control_results'] == after.get('control_results')
                and baseline['device_results'] == after.get('device_results')):
            p.error('native baseline does not match this probe, driver or completed experiment')
        report['native_baseline'] = str(a.native_baseline.resolve())
    os.sched_setaffinity(0, [0, 1, 2, 3])
    report['cpu_affinity'] = sorted(os.sched_getaffinity(0))
    with (out / 'commands.log').open('x') as log:
        def run(command, payload=None, timeout=180):
            log.write('$ ' + shlex.join(command) + '\n'); log.flush()
            r = subprocess.run(command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout)
            text = r.stdout.decode(errors='replace')
            log.write(text + f'\n[exit={r.returncode}]\n'); log.flush()
            return r.returncode, text
        def require(command, payload=None):
            rc, text = run(command, payload)
            if rc: raise RuntimeError('command failed: ' + shlex.join(command))
            return text
        def validate_control(name, rc, text):
            rows = re.findall(r'^' + marker + r'_CHECK (\w+) (pass|fail)$', text, re.M)
            passed = (rc == 0 and len(rows) == len(expected_checks)
                      and {n for n, status in rows if status == 'pass'} == expected_checks
                      and (fd_mode or rm_mode or ctrl_mode or bv_mode or f'GPU_CONTROL_VERSION {version}' in text)
                      and marker + '_END status=0 internal_cuda_verified=0' in text)
            contract = {}
            if ctrl_mode or bv_mode:
                contract['control_results'] = [json.loads(row) for row in re.findall(r'^' + marker + r'_RESULT (.+)$', text, re.M)]
                contract['device_results'] = [json.loads(row) for row in re.findall(r'^GPU_CTRL_DEVICE (.+)$', text, re.M)]
                contract['native_contract_matches'] = (
                    contract['control_results'] == baseline['control_results']
                    and contract['device_results'] == baseline['device_results'])
                passed = passed and contract['native_contract_matches']
            report['checks'][name] = 'pass' if passed else 'fail'
            report['probe_results'][name] = {'exit_code': rc, 'checks': rows,
                                             'passed': passed, 'expected_checks': sorted(expected_checks), **contract}
            # RM parity failures remain failures; finish the lifecycle observations
            # so unsupported commands do not hide restart/concurrency evidence.
            if not passed and not (rm_mode or ctrl_mode or bv_mode): raise RuntimeError(name + ' failed')
        def control_check(name, command):
            validate_control(name, *run(command))
        try:
            start = base + ['start', '--kernel', str(a.kernel.resolve()), '--disk', str(a.disk.resolve()),
                            *(['--memfd-ram'] if a.memfd_ram else []),
                            *(['--memfd-stub'] if a.memfd_stub else []),
                            *(['--gpu-async-wait'] if a.async_wait else []), '--gpu-control-experiment']
            require(start)
            report['ready_state'] = harddind.read_state(instance)
            if a.memfd_ram or a.memfd_stub:
                log_text = (instance / ('guest-' + report['ready_state']['run'] + '.log')).read_text(errors='replace')
                report['memory_backing'] = memfd_backing_evidence(log_text, a.memfd_ram, a.memfd_stub)
                report['checks']['memory_backing'] = 'pass' if report['memory_backing']['verified'] else 'fail'
            mismatch_flag = '--gpu-async-wait' if a.async_wait else '--gpu-control-experiment'
            rc, text = run([part for part in start if part != mismatch_flag])
            report['checks']['configuration_mismatch_rejected'] = 'pass' if (
                rc != 0 and 'running instance uses different inputs' in text) else 'fail'
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode='w') as archive:
                for name in ['dev', 'proc', 'tmp']:
                    entry = tarfile.TarInfo(name); entry.type = tarfile.DIRTYPE; entry.mode = 0o755
                    archive.addfile(entry)
                archive.add(a.probe.resolve(), arcname='gpu-guest-probe', recursive=False)
            report['image_id'] = require(base + ['docker', 'import', '-', image], bundle.getvalue()).strip()
            probe_args = [image, '/gpu-guest-probe',
                          '/dev/harddind-nvidia' if fd_mode or rm_mode or ctrl_mode or bv_mode else '/dev/harddind-nvidiactl',
                          str(a.gpu_count) if fd_mode or rm_mode or ctrl_mode or bv_mode else version]
            rc, text = run(base + ['docker', 'run', '--rm', *probe_args])
            report['checks']['no_device'] = 'pass' if rc == 1 and 'No such file or directory' in text else 'fail'
            control_check('device_control', base + ['docker', 'run', '--name', container,
                                                   *devices, *probe_args])
            for name, extra, error in [
                ('nonroot_denied', ['--device=/dev/harddind-nvidiactl', '--user=1000:1000'], 'Permission denied'),
                ('device_readonly_denied', ['--device=/dev/harddind-nvidiactl:/dev/harddind-nvidiactl:r'], 'Operation not permitted'),
            ]:
                rc, text = run(base + ['docker', 'run', '--rm', *extra, *probe_args])
                report['checks'][name] = 'pass' if rc == 1 and error in text else 'fail'
            control_check('container_restart', base + ['docker', 'start', '-a', container])
            require(base + ['docker', 'rm', container])
            clients = []
            try:
                for label in ['a', 'b']:
                    command = base + ['docker', 'run', '--rm', '--name', container + '-' + label,
                                      *devices, *probe_args]
                    log.write('$ ' + shlex.join(command) + '\n'); log.flush()
                    clients.append((label, subprocess.Popen(command, stdout=subprocess.PIPE,
                                                             stderr=subprocess.STDOUT)))
                for label, proc in clients:
                    data, _ = proc.communicate(timeout=180)
                    text = data.decode(errors='replace')
                    log.write(f'[client {label}]\n{text}\n[exit={proc.returncode}]\n'); log.flush()
                    validate_control('concurrent_client_' + label, proc.returncode, text)
            finally:
                for _, proc in clients:
                    if proc.poll() is None:
                        proc.terminate()
                        try: proc.wait(timeout=10)
                        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            require(base + ['stop'])
            report['restart_shutdown'] = harddind.read_state(instance)
            if report['restart_shutdown'].get('phase') != 'stopped':
                raise RuntimeError('first instance shutdown failed')
            require(start)
            report['restarted_state'] = harddind.read_state(instance)
            control_check('instance_restart', base + ['docker', 'run', '--rm',
                                                       *devices, *probe_args])
            report['checks']['inputs_unchanged'] = 'pass' if all(
                harddind.sha(f) == report['sha256'][str(f)] for f in inputs) else 'fail'
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
        finally:
            try:
                state = harddind.read_state(instance)
                if harddind.live(state) and state.get('phase') == 'ready':
                    run(base + ['docker', 'rm', '-f', container, container + '-a', container + '-b'], timeout=30)
                rc, _ = run(base + ['stop'], timeout=75)
                report['final_state'] = harddind.read_state(instance)
                state = report['final_state']
                report['checks']['normal_shutdown'] = 'pass' if (
                    rc == 0 and state.get('phase') == 'stopped' and not state.get('forced_shutdown')
                    and state.get('guest_cleanup', {}).get('clean_shutdown')
                    and state.get('network_cleanup', {}).get('clean_shutdown')) else 'fail'
            except Exception as error:
                report['shutdown_error'] = str(error)
            saved = out / 'instance'; saved.mkdir()
            for f in instance.glob('*'):
                if f.is_file() and f.suffix in ('.log', '.json'):
                    shutil.copy2(f, saved / f.name)
            if a.async_wait:
                console = '\n'.join(f.read_text(errors='replace') for f in saved.glob('guest-*.log'))
                workers = re.findall(r'HARDDIND_GPU_WORKER guest_tgid=\d+ host_pid=(\d+)', console)
                async_workers = re.findall(r'HARDDIND_GPU_ASYNC guest_tgid=\d+ host_pid=(\d+) irq=\d+', console)
                report['async_workers'] = async_workers
                report['gpu_workers'] = workers
                report['checks']['async_wait_active'] = 'pass' if len(workers) == 25 and len(async_workers) == 25 and set(workers) == set(async_workers) else 'fail'
            report['diagnostic_complete'] = (not report.get('error') and not report.get('shutdown_error')
                                             and all(v == 'pass' for v in report['checks'].values()))
            (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(out / 'report.json'), 'checks': report['checks'],
                      'error': report.get('error'), 'diagnostic_complete': report['diagnostic_complete']}, indent=2))
    return 0 if report['diagnostic_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
