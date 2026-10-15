#!/usr/bin/env python3
"""Functional verification of real Docker CPU limits through the persistent
harddind daemon and the stock Docker CLI.

This is an enforcement test, not a benchmark: busy threads must actually be
capped by --cpus / --cpu-period / --cpu-quota / --cpuset-cpus, a live
`docker update` must change enforcement inside the same running container, and
every verdict is backed by cgroup cpu.stat throttling counters, per-thread and
process CPU clocks, affinity masks and CFS period-accounting bounds.  Inspected
config fields are recorded but never accepted as enforcement evidence.

Reads the shared outer cgroup only; it never creates host cgroups, pins host
CPUs, or runs a native load.  Guest spin time is bounded (default ~48 s total)
and the owner schedules execution under the global kernel-build.lock protocol.
"""
import argparse
import datetime
import fcntl
import io
import json
import os
import pathlib
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import harddind
import cpu_limit_checks as checks
from guest_cleanup import cleanup_group

IMAGE = 'harddind-cpu-limits:local'
PROBE_PATH = '/cpu-limit-probe'
CONTAINERS = ['cpulim-ref', 'cpulim-cpus1', 'cpulim-explicit', 'cpulim-cpuset0',
              'cpulim-cpuset-range', 'cpulim-cpuset-quota', 'cpulim-update']
UPDATE_MS = 20000
UPDATE_AT = (5.0, 12.0)
UPDATE_SCHEDULE = [(2.0, 100000), (0.5, 100000), (2.0, 100000)]
INSPECT_TEMPLATE = ('{{.HostConfig.NanoCpus}}|{{.HostConfig.CpuPeriod}}|'
                    '{{.HostConfig.CpuQuota}}|{{.HostConfig.CpusetCpus}}|'
                    '{{.Id}}|{{.State.Running}}|{{.State.ExitCode}}')
INFO_TEMPLATE = 'ncpu={{.NCPU}} server={{.ServerVersion}} kernel={{.KernelVersion}} driver={{.Driver}} cgroup={{.CgroupVersion}}'

HOST_READ_FILES = (('loadavg', '/proc/loadavg'),
                   ('cpu.pressure', '/sys/fs/cgroup/cpu.pressure'),
                   ('cpu.stat', '/sys/fs/cgroup/cpu.stat'),
                   ('cpu.max', '/sys/fs/cgroup/cpu.max'),
                   ('cpuset.cpus.effective', '/sys/fs/cgroup/cpuset.cpus.effective'))


def host_state():
    state = {'nproc': os.cpu_count()}
    for label, path in HOST_READ_FILES:
        try:
            state[label] = pathlib.Path(path).read_text().strip()
        except OSError as error:
            state[label] = f'<{type(error).__name__}: {error}>'
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True, help='immutable experiment name; reuse is refused')
    p.add_argument('--cpus', type=int, default=4, help='guest vCPU count (1..64)')
    p.add_argument('--window-ms', type=int, default=6000, help='fixed-quota phase duration (2000..30000)')
    p.add_argument('--sample-ms', type=int, default=100, help='probe sample interval (50..500)')
    a = p.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,27}', a.name):
        p.error('name must match [a-z0-9][a-z0-9_-]{0,27}')
    if not 1 <= a.cpus <= 64:
        p.error('cpus must be between 1 and 64')
    if not 2000 <= a.window_ms <= 30000 or not 50 <= a.sample_ms <= 500:
        p.error('invalid window or sample timing')
    work = a.work.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    source = repo / 'src/cpu_limit_probe.c'
    checker = repo / 'scripts/cpu_limit_checks.py'
    out = work / 'results' / a.name
    instance = work / 'instances' / a.name
    if instance.exists() or out.exists():
        p.error('refusing existing instance or evidence: ' + str(instance))
    out.mkdir(parents=True, exist_ok=False)
    probe_bin = out / 'cpu-limit-probe'
    report = {
        'kind': 'docker_cpu_limit_enforcement',
        'experiment': a.name,
        'full_acceptance': False,
        'performance_acceptance': False,
        'runtime_verification': 'not_tested',
        'mode': 'functional_enforcement',
        'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'identity': {'hostname': platform.node(), 'kernel': platform.release(),
                     'machine': platform.machine(), 'cwd': os.getcwd(), 'uid': os.getuid()},
        'params': {'guest_cpus': a.cpus, 'window_ms': a.window_ms, 'sample_ms': a.sample_ms,
                   'update_ms': UPDATE_MS, 'update_at_s': list(UPDATE_AT)},
        'host_before': host_state(),
        'phases': {},
        'checks': {},
        'limitations': [
            'functional enforcement only; no near-native performance threshold is applied',
            'cpu.weight/--cpu-shares contention between co-running containers is a follow-up',
            'outer cgroup inspected read-only; no native CPU baseline is run',
            'single daemon guest; swarm/cgroup-v1 and live cpu-period changes are not covered'],
        'followups': {
            'cpu_weight_share_contention': {
                'status': 'not_tested',
                'reason': 'needs a controlled two-container contention protocol; never counts as passed'}},
        'errors': [],
        'commands': [],
    }

    def note_error(label, error):
        report['errors'].append({'where': label, 'error': f'{type(error).__name__}: {error}'})

    inputs = [a.kernel.resolve(), a.disk.resolve(), source, pathlib.Path(__file__).resolve(),
              checker, repo / 'scripts/harddind.py', repo / 'scripts/guest_cleanup.py']
    report['sha256'] = {str(f): harddind.sha(f) for f in inputs if f.exists()}
    base = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), a.name]

    with (out / 'commands.log').open('x') as commands:
        def run(label, cmd, *, stdin=None, timeout=90):
            commands.write('$ ' + shlex.join(cmd) + '\n')
            commands.flush()
            started = time.monotonic()
            with (out / (label + '.stdout')).open('xb') as so, (out / (label + '.stderr')).open('xb') as se:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                timed_out = False
                try:
                    stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate(timeout=15)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        stdout, stderr = proc.communicate(timeout=15)
                    audit = cleanup_group(proc.pid)
                so.write(stdout or b'')
                se.write(stderr or b'')
            row = {'label': label, 'command': cmd, 'exit_code': proc.returncode,
                   'timed_out': timed_out, 'elapsed_s': time.monotonic() - started,
                   'stdout': (stdout or b'').decode(errors='replace'),
                   'stderr': (stderr or b'').decode(errors='replace'),
                   'cleanup': audit}
            commands.write(f'[{label} exit={proc.returncode} timeout={timed_out}]\n')
            commands.flush()
            report['commands'].append({k: v for k, v in row.items() if k not in ('stdout', 'stderr')})
            return row

        def cli(label, *args, **kwargs):
            return run(label, base + ['docker', *args], **kwargs)

        def require(row, what):
            if row['exit_code'] != 0 or row['timed_out'] or not row['cleanup']['clean_shutdown']:
                raise RuntimeError(what + ': ' + row['label'])
            return row

        def checked_cli(label, *args, **kwargs):
            return require(cli(label, *args, **kwargs), label)

        def build_probe():
            cmd = ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                   str(source), '-o', str(probe_bin)]
            row = run('build-probe', cmd, timeout=60)
            require(row, 'probe compilation')
            ver = run('gcc-version', ['gcc', '--version'], timeout=15)
            report['compiler'] = ver['stdout'].splitlines()[0] if ver['exit_code'] == 0 else 'unknown'
            interp = run('probe-static-check', ['readelf', '-l', str(probe_bin)], timeout=15)
            require(interp, 'probe static inspection')
            if re.search(r'\bINTERP\b', interp['stdout']):
                raise RuntimeError('probe is not a static binary')
            selftest = run('probe-selftest', [str(probe_bin), 'selftest'], timeout=15)
            require(selftest, 'probe selftest')
            if json.loads(selftest['stdout']).get('status') != 'pass':
                raise RuntimeError('probe selftest reported failure')
            report['sha256'][str(probe_bin)] = harddind.sha(probe_bin)

        def import_image():
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode='w') as archive:
                for directory in ('dev', 'proc', 'sys', 'tmp'):
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                info = archive.gettarinfo(str(probe_bin), arcname=PROBE_PATH.lstrip('/'))
                info.mode = 0o755
                info.uname = info.gname = 'root'
                with probe_bin.open('rb') as f:
                    archive.addfile(info, f)
            row = cli('image-import', 'import', '-', IMAGE, stdin=bundle.getvalue(), timeout=120)
            require(row, 'image import')
            report['image_id'] = row['stdout'].strip()

        def parse_inspect(text):
            parts = text.rstrip('\n').split('|')
            if len(parts) < 7:
                raise ValueError('incomplete docker inspect output: ' + text.strip())
            return {'nano_cpus': int(parts[0]), 'cpu_period': int(parts[1]), 'cpu_quota': int(parts[2]),
                    'cpuset_cpus': parts[3], 'id': parts[4], 'running': parts[5] == 'true',
                    'exit_code': int(parts[6])}

        def wait_running(name, deadline_s=12.0):
            until = time.monotonic() + deadline_s
            attempt = 0
            while time.monotonic() < until:
                r = checked_cli(f'poll-{name}-{attempt}', 'inspect', '-f',
                                '{{.State.Running}} {{.Id}}', name, timeout=20)
                attempt += 1
                if r['exit_code'] == 0:
                    running, cid = r['stdout'].split()
                    if running == 'true':
                        return cid
                time.sleep(0.2)
            raise RuntimeError(name + ' did not reach running state')

        def run_spin_phase(spec, ncpu):
            name = spec['name']
            run('prerm-' + name, base + ['docker', 'rm', '-f', name], timeout=20)
            spin = [PROBE_PATH, 'spin', '--threads', str(ncpu),
                    '--duration-ms', str(a.window_ms), '--sample-ms', str(a.sample_ms)]
            row = cli('start-' + name, 'run', '-d', '--name', name, *spec['flags'], IMAGE, *spin, timeout=40)
            require(row, 'start ' + name)
            created_id = row['stdout'].strip()
            if wait_running(name) != created_id:
                raise RuntimeError('container changed during startup')
            cfg = parse_inspect(checked_cli('config-' + name, 'inspect', '-f', INSPECT_TEMPLATE, name, timeout=20)['stdout'])
            wait_row = checked_cli('wait-' + name, 'wait', name, timeout=a.window_ms / 1000 + 60)
            final = parse_inspect(checked_cli('final-' + name, 'inspect', '-f', INSPECT_TEMPLATE, name, timeout=20)['stdout'])
            logs = checked_cli('logs-' + name, 'logs', name, timeout=30)
            rm = checked_cli('rm-' + name, 'rm', name, timeout=30)
            probe_exit = wait_row['stdout'].strip()
            try:
                events = checks.parse_events(logs['stdout'])
                summary = checks.spin_summary(events)
                parse_error = None
            except Exception as error:
                events, summary, parse_error = [], None, f'{type(error).__name__}: {error}'
            phase = {'spec': {k: v for k, v in spec.items()},
                     'created_id': created_id, 'wait_exit': wait_row['exit_code'],
                     'probe_exit': probe_exit, 'inspect_start': cfg,
                     'inspect_end': final, 'removed': rm['exit_code'] == 0,
                     'parse_error': parse_error, 'events': events}
            report['phases'][spec['phase']] = phase
            if (probe_exit != '0' or final['exit_code'] != 0 or final['running']
                    or cfg['id'] != created_id or final['id'] != created_id):
                phase['parse_error'] = 'container identity or exit status failed'
                return None
            return summary

        def run_update_phase(ncpu):
            name = 'cpulim-update'
            run('prerm-' + name, base + ['docker', 'rm', '-f', name], timeout=20)
            spin = [PROBE_PATH, 'spin', '--threads', str(ncpu),
                    '--duration-ms', str(UPDATE_MS), '--sample-ms', str(a.sample_ms)]
            created = cli('start-' + name, 'run', '-d', '--name', name, '--cpus', '2.0', IMAGE, *spin, timeout=40)
            require(created, 'start update container')
            created_id = created['stdout'].strip()
            if wait_running(name) != created_id:
                raise RuntimeError('update container changed during startup')
            snapshots = []
            t0 = time.monotonic()
            identities = [created_id]
            for i, target in enumerate(('0.5', '2.0')):
                when = t0 + UPDATE_AT[i]
                while time.monotonic() < when:
                    time.sleep(0.05)
                upd = cli('update-' + str(i), 'update', '--cpus', target, name, timeout=20)
                require(upd, 'docker update ' + target)
                snap = checked_cli('snapshot-' + str(i), 'exec', name, PROBE_PATH, 'snapshot', timeout=20)
                cfg = parse_inspect(checked_cli('postupdate-' + str(i), 'inspect', '-f', INSPECT_TEMPLATE, name, timeout=20)['stdout'])
                identities.append(cfg.get('id'))
                snap_event = checks.parse_events(snap['stdout'])
                snapshots.append({'target_cpus': target, 'snapshot': snap_event[0] if snap_event else None,
                                  'inspect': cfg, 'update_exit': upd['exit_code']})
            waited = checked_cli('wait-' + name, 'wait', name, timeout=40)
            final = parse_inspect(checked_cli('final-' + name, 'inspect', '-f', INSPECT_TEMPLATE, name, timeout=20)['stdout'])
            identities.append(final.get('id'))
            logs = checked_cli('logs-' + name, 'logs', name, timeout=30)
            checked_cli('rm-' + name, 'rm', name, timeout=30)
            try:
                events = checks.parse_events(logs['stdout'])
                summary = checks.spin_summary(events)
                parse_error = None
            except Exception as error:
                events, summary, parse_error = [], None, f'{type(error).__name__}: {error}'
            report['phases']['live_update'] = {
                'created_id': created_id, 'wait_exit': waited['exit_code'],
                'wait_output': waited['stdout'].strip(), 'snapshots': snapshots,
                'inspect_end': final, 'identities': identities,
                'same_container': len(set(identities)) == 1 and created_id == final.get('id'),
                'parse_error': parse_error, 'events': events}
            if waited['stdout'].strip() != '0' or final['exit_code'] != 0 or final['running']:
                report['phases']['live_update']['parse_error'] = 'update container did not exit successfully'
                return None
            return summary

        # Everything stays inside the commands.log context so command evidence
        # is flushed even when the run aborts early.
        try:
            build_probe()
            lock_f = (work / 'kernel-build.lock').open('a')
            fcntl.flock(lock_f, fcntl.LOCK_SH)
            start_row = run('instance-start', base + ['start', '--kernel', str(a.kernel.resolve()),
                                                       '--disk', str(a.disk.resolve()),
                                                       '--cpus', str(a.cpus)], timeout=180)
            require(start_row, 'instance start')
            info_row = cli('daemon-info', 'info', '--format', INFO_TEMPLATE, timeout=30)
            require(info_row, 'docker info')
            report['daemon_info'] = info_row['stdout'].strip()
            m = re.search(r'ncpu=(\d+)', report['daemon_info'])
            if not m or int(m.group(1)) != a.cpus:
                raise RuntimeError('daemon NCPU does not match requested guest cpus')
            import_image()

            ncpu = a.cpus
            ref_summary = run_spin_phase(
                {'phase': 'unthrottled_reference', 'name': 'cpulim-ref',
                 'flags': [], 'kind': 'reference'}, ncpu)
            if ref_summary is None:
                report['checks']['unthrottled_reference'] = {
                    'status': checks.FAIL,
                    'reason': 'reference probe produced no parseable evidence',
                    'evidence': {}}
            else:
                report['checks']['unthrottled_reference'] = checks.classify_reference(ref_summary, ncpu)
            ref = ref_summary

            fixed_specs = [
                {'phase': 'cpus_flag_quota', 'name': 'cpulim-cpus1',
                 'flags': ['--cpus', '1.0'], 'kind': 'quota', 'cores': 1.0, 'period_s': 0.1},
                {'phase': 'explicit_period_quota', 'name': 'cpulim-explicit',
                 'flags': ['--cpu-period=50000', '--cpu-quota=25000'],
                 'kind': 'quota', 'cores': 0.5, 'period_s': 0.05},
                {'phase': 'cpuset_single', 'name': 'cpulim-cpuset0',
                 'flags': ['--cpuset-cpus=0'], 'kind': 'cpuset', 'cpus': [0]},
            ]
            if ncpu >= 3:
                fixed_specs.append({'phase': 'cpuset_range', 'name': 'cpulim-cpuset-range',
                                    'flags': ['--cpuset-cpus=1-2'], 'kind': 'cpuset', 'cpus': [1, 2]})
            else:
                report['checks']['cpuset_range'] = {
                    'status': checks.NOT_TESTED,
                    'reason': f'guest provides {ncpu} vCPU; range 1-2 needs at least 3'}
            if ncpu >= 2:
                fixed_specs.append({'phase': 'cpuset_with_quota', 'name': 'cpulim-cpuset-quota',
                                    'flags': ['--cpuset-cpus=0', '--cpus', '0.5'],
                                    'kind': 'cpuset_quota', 'cpus': [0],
                                    'cores': 0.5, 'period_s': 0.1})
            else:
                report['checks']['cpuset_with_quota'] = {
                    'status': checks.NOT_TESTED,
                    'reason': f'guest provides {ncpu} vCPU; quota-on-cpuset test needs at least 2'}

            for spec in fixed_specs:
                summary = run_spin_phase(spec, ncpu)
                phase_row = report['phases'][spec['phase']]
                if summary is None:
                    result = {'status': checks.FAIL,
                              'reason': 'probe produced no parseable evidence: ' + str(phase_row['parse_error']),
                              'evidence': {'probe_exit': phase_row['probe_exit']}}
                elif spec['kind'] == 'quota':
                    result = checks.classify_quota(summary, spec['cores'], spec['period_s'], ref)
                elif spec['kind'] == 'cpuset':
                    result = checks.classify_cpuset(summary, spec['cpus'])
                else:
                    result = checks.classify_cpuset(summary, spec['cpus'],
                                                    expected_quota=spec['cores'],
                                                    period_s=spec['period_s'], reference=ref)
                report['checks'][spec['phase']] = result

            update_summary = run_update_phase(ncpu)
            if update_summary is None:
                for label in ('update_window_initial_2', 'update_window_half', 'update_window_recovered_2'):
                    report['checks'][label] = {
                        'status': checks.FAIL,
                        'reason': 'update probe produced no parseable evidence: '
                                  + str(report['phases']['live_update']['parse_error']),
                        'evidence': {}}
                window_results = []
            else:
                window_results = checks.classify_update_run(update_summary, UPDATE_SCHEDULE, ref)
                labels = ['update_window_initial_2', 'update_window_half', 'update_window_recovered_2']
                for label, result in zip(labels, window_results):
                    report['checks'][label] = result
            timeline = report['phases']['live_update']
            expected_raws = ['200000 100000', '50000 100000', '200000 100000']
            observed_raws = []
            for s in timeline['snapshots']:
                raw = (s.get('snapshot') or {}).get('cgroup', {}).get('cpu.max', {}).get('raw')
                observed_raws.append(raw)
            report['checks']['update_timeline'] = {
                'status': checks.PASS if observed_raws == expected_raws[1:] else checks.FAIL,
                'reason': 'docker exec snapshots bracket each docker update',
                'evidence': {'expected_after_update': expected_raws[1:], 'observed': observed_raws}}
            report['checks']['same_container_identity'] = {
                'status': checks.PASS if timeline['same_container'] else checks.FAIL,
                'reason': 'updates and snapshots all target the container created at run -d',
                'evidence': {'identities': timeline['identities']}}

            # Config-write evidence: recorded for every phase, explicitly NOT
            # the basis of any enforcement verdict.
            config_rows = []
            for phase_name, phase in report['phases'].items():
                if phase_name == 'live_update' or not phase.get('events'):
                    continue
                raw = phase['events'][-1]['cgroup']['cpu.max'].get('raw')
                config_rows.append({'phase': phase_name,
                                    'inspect': {k: phase['inspect_start'].get(k)
                                                for k in ('nano_cpus', 'cpu_period', 'cpu_quota', 'cpuset_cpus')},
                                    'in_container_cpu_max': raw})
            report['checks']['config_writes_recorded'] = {
                'status': checks.PASS if len(config_rows) == 1 + len(fixed_specs) else checks.FAIL,
                'reason': 'docker inspect and in-container cpu.max recorded alongside enforcement evidence',
                'evidence': {'rows': config_rows, 'note': 'configuration evidence only'}}

            stop_row = run('instance-stop', base + ['stop'], timeout=90)
            report['stop_exit'] = stop_row['exit_code']
        except Exception as error:
            note_error('run', error)
        finally:
            try:
                state = harddind.read_state(instance)
                if harddind.live(state):
                    if state.get('phase') == 'ready':
                        for name in CONTAINERS:
                            run('cleanup-' + name, base + ['docker', 'rm', '-f', name], timeout=20)
                    run('final-stop', base + ['stop'], timeout=90)
                report['final_state'] = harddind.read_state(instance)
            except Exception as error:
                note_error('cleanup', error)
            saved = out / 'instance'
            saved.mkdir(exist_ok=True)
            if instance.exists():
                for f in instance.iterdir():
                    if f.is_file() and f.suffix in ('.json', '.log'):
                        shutil.copy2(f, saved / f.name)
            report['host_after'] = host_state()
            report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            final_state = report.get('final_state', {})
            lifecycle_pass = (final_state.get('phase') == 'stopped'
                              and not final_state.get('forced_shutdown')
                              and final_state.get('guest_cleanup', {}).get('clean_shutdown')
                              and final_state.get('network_cleanup', {}).get('clean_shutdown')
                              and all(c['cleanup']['clean_shutdown'] and not c['timed_out']
                                      for c in report['commands']))
            report['checks']['lifecycle_clean'] = {
                'status': checks.PASS if lifecycle_pass else checks.FAIL,
                'reason': 'persistent guest stopped cleanly with exact process-group cleanup',
                'evidence': {'phase': final_state.get('phase'),
                             'forced_shutdown': final_state.get('forced_shutdown')}}
            report['checks']['inputs_unchanged'] = {
                'status': checks.PASS if all(pathlib.Path(path).is_file() and harddind.sha(pathlib.Path(path)) == sha
                                             for path, sha in report['sha256'].items()) else checks.FAIL,
                'reason': 'kernel, disk, probe source/binary and scripts unchanged after run'}
            report['checks']['cpu_weight_share_contention'] = report['followups']['cpu_weight_share_contention']

            required = ['lifecycle_clean', 'inputs_unchanged', 'config_writes_recorded',
                        'unthrottled_reference', 'cpus_flag_quota', 'explicit_period_quota',
                        'cpuset_single', 'cpuset_range', 'cpuset_with_quota',
                        'update_window_initial_2', 'update_window_half',
                        'update_window_recovered_2', 'update_timeline', 'same_container_identity']
            statuses = [report['checks'].get(k, {}).get('status') for k in required]
            missing = [k for k in required if report['checks'].get(k, {}).get('status') is None]
            any_fail = (checks.FAIL in statuses) or bool(report['errors']) or bool(missing)
            any_indeterminate = any(s != checks.PASS for s in statuses)
            if not any_fail and not any_indeterminate:
                verdict = checks.PASS
            elif any_fail:
                verdict = checks.FAIL
            else:
                verdict = checks.INDETERMINATE
            report['verdict'] = verdict
            report['runtime_verification'] = verdict
            (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    print(json.dumps({'experiment': a.name, 'verdict': report['verdict'],
                      'checks': {k: v.get('status') for k, v in report['checks'].items()},
                      'result': str(out / 'report.json'),
                      'errors': report['errors']}, indent=2))
    if verdict == checks.PASS:
        return 0
    return 1 if verdict == checks.FAIL else 2


if __name__ == '__main__':
    raise SystemExit(main())
