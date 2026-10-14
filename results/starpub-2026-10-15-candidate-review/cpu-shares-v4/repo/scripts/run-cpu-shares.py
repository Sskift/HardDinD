#!/usr/bin/env python3
"""Functional verification that Docker --cpu-shares changes real CPU allocation.

v2 of the shares experiment (v1 was rejected; see owner-review-v1). Two stock
nested Docker containers inside the persistent harddind guest run the dedicated
`contend` mode of the OWN probe copy (src/cpu_share_probe.c): several always
busy threads each, both pinned with --cpuset-cpus to one shared guest vCPU, no
quota. Plan on unchanged container IDs:

  stage 0  A=1024  B=512                 (heavy vs light)
  stage 1  A=512   B=1024  live update   (reversed)
  stage 2  A=512   B=512   live update   (equal control)

Evidence timing is owned by bracketed captures (frozen v4 capture contract
plus weight brackets): every contend sample stamps wall_ns before the first
syscall and cap_end_ns after the last, reading cpu.max/cpu.weight/cpuset
BEFORE and AFTER cpu.stat. A weight change landing inside a (possibly
multi-second) capture is reported weight_stable=0 and quarantined; rate deltas
span only stable-fast joint intervals and are scored over the full
[narrow, wide] plausible-duration box. The checker intersects BOTH containers'
run epochs; expected fractions come from the observed cgroup weights, never
from raw shares (the pinned daemon converts nonlinearly). Pre/post exec
snapshots bracket each update as corroboration.

Only shared outer cgroup files are read; no host cgroups are created, no host
CPUs pinned, no native load run. Guest spin time bounded (~32 s default).
The JSON report is always written, including on failure.
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
import cpu_share_checks as checks
from guest_cleanup import cleanup_group

IMAGE = 'harddind-cpu-shares:local'
PROBE_PATH = '/cpu-share-probe'
PROBE_SOURCE = 'src/cpu_share_probe.c'
NAMES = {'a': 'cpushare-a', 'b': 'cpushare-b'}
CONTAINERS = ('a', 'b')
PLAN = checks.PLAN
HEAD_MS = 5000
INSPECT_TEMPLATE = ('{{.HostConfig.CpuShares}}|{{.HostConfig.CpusetCpus}}|'
                    '{{.HostConfig.CpuQuota}}|{{.HostConfig.CpuPeriod}}|'
                    '{{.Id}}|{{.State.Running}}|{{.State.ExitCode}}')
INFO_TEMPLATE = 'ncpu={{.NCPU}} server={{.ServerVersion}} kernel={{.KernelVersion}} driver={{.Driver}} cgroup={{.CgroupVersion}}'

HOST_READ_FILES = (('loadavg', '/proc/loadavg'),
                   ('cpu.pressure', '/sys/fs/cgroup/cpu.pressure'),
                   ('cpu.stat', '/sys/fs/cgroup/cpu.stat'),
                   ('cpu.weight', '/sys/fs/cgroup/cpu.weight'),
                   ('cpuset.cpus.effective', '/sys/fs/cgroup/cpuset.cpus.effective'))


def host_state():
    state = {'nproc': os.cpu_count()}
    for label, path in HOST_READ_FILES:
        try:
            state[label] = pathlib.Path(path).read_text().strip()
        except OSError as error:
            state[label] = '<%s: %s>' % (type(error).__name__, error)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True, help='immutable experiment name; reuse is refused')
    p.add_argument('--cpus', type=int, default=4, help='guest vCPU count (protocol requires 4)')
    p.add_argument('--shared-cpu', type=int, default=2, help='guest CPU both containers contend for')
    p.add_argument('--threads', type=int, default=4, help='busy threads per container (2..16)')
    p.add_argument('--phase-ms', type=int, default=9000, help='stage spacing (5000..20000)')
    p.add_argument('--sample-ms', type=int, default=100, help='probe sample interval (50..500)')
    a = p.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,27}', a.name):
        p.error('name must match [a-z0-9][a-z0-9_-]{0,27}')
    if a.cpus != 4:
        p.error('this protocol requires a 4-vCPU guest (got %d)' % a.cpus)
    if not 0 <= a.shared_cpu < a.cpus:
        p.error('shared cpu must be in 0..%d' % (a.cpus - 1))
    if not 2 <= a.threads <= 16 or not 5000 <= a.phase_ms <= 20000 or not 50 <= a.sample_ms <= 500:
        p.error('invalid thread/phase/sample parameters')
    work = a.work.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    source = repo / PROBE_SOURCE
    checker = repo / 'scripts/cpu_share_checks.py'
    runner_path = pathlib.Path(__file__).resolve()
    out = work / 'results' / a.name
    instance = work / 'instances' / a.name
    if instance.exists() or out.exists():
        p.error('refusing existing instance or evidence: ' + str(instance))
    out.mkdir(parents=True, exist_ok=False)
    probe_bin = out / 'cpu-share-probe'
    duration_ms = HEAD_MS + 3 * a.phase_ms
    if duration_ms > 120000:
        p.error('contend duration would exceed probe limit')
    started_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        'kind': 'docker_cpu_share_contention',
        'schema_version': 3,
        'experiment': a.name,
        'full_acceptance': False,
        'performance_acceptance': False,
        'runtime_verification': 'not_tested',
        'mode': 'functional_allocation',
        'started_at': started_iso,
        'identity': {'hostname': platform.node(), 'kernel': platform.release(),
                     'machine': platform.machine(), 'cwd': os.getcwd(), 'uid': os.getuid()},
        'params': {'guest_cpus': a.cpus, 'shared_cpu': a.shared_cpu, 'threads': a.threads,
                   'phase_ms': a.phase_ms, 'sample_ms': a.sample_ms,
                   'contend_duration_ms': duration_ms, 'head_ms': HEAD_MS,
                   'start_trim_s': checks.START_TRIM_S, 'min_window_s': checks.MIN_WINDOW_S,
                   'min_samples': checks.MIN_SAMPLES, 'max_gap_s': checks.MAX_GAP_S,
                   'endpoint_skew_s': checks.ENDPOINT_SKEW_S,
                   'bracket_skew_s': checks.BRACKET_SKEW_S,
                   'capture_fast_s': checks.CAPTURE_FAST_S,
                   'min_phase_coverage': checks.MIN_PHASE_COVERAGE,
                   'plan': [dict(s) for s in PLAN]},
        'host_before': host_state(),
        'evidence': {},
        'checks': {},
        'limitations': [
            'functional CPU-share allocation only; no near-native or workload throughput threshold',
            'single shared guest vCPU contention fixture on a 4-vCPU guest; not a single-CPU product claim',
            'outer cgroup inspected read-only; no native CPU load is run on the host',
            'one daemon, cgroup v2; swarm and cgroup v1 share semantics are not covered',
            'own probe copy src/cpu_share_probe.c pending owner merge with round10 sampler repairs'],
        'errors': [],
        'commands': [],
    }

    def note_error(label, error):
        report['errors'].append({'where': label, 'error': '%s: %s' % (type(error).__name__, error)})

    role_files = [
        ('kernel', a.kernel.resolve()), ('disk', a.disk.resolve()),
        ('probe_source', source), ('probe_binary', probe_bin),
        ('runner', runner_path), ('checker', checker),
        ('harddind', repo / 'scripts/harddind.py'),
        ('guest_cleanup', repo / 'scripts/guest_cleanup.py')]

    def hash_inputs():
        rows = []
        for role, path in role_files:
            if path.exists():
                rows.append({'role': role, 'path': str(path), 'sha256': harddind.sha(path)})
        return rows

    base = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), a.name]
    cdata = {c: {'name': NAMES[c], 'snapshots': [], 'inspections': [],
                 'spin_events': [], 'spin_error': None}
             for c in CONTAINERS}
    ev = {'params': report['params'], 'daemon': {},
          'containers': {c: {'name': NAMES[c]} for c in CONTAINERS},
          'updates': [], 'commands': [],
          'inputs': [], 'leftovers_stdout': None, 'final_state': {}}

    with (out / 'commands.log').open('x') as commands:
        def run(label, cmd, *, stdin=None, timeout=90):
            issued_mono = time.monotonic()
            issued_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
            commands.write('$ ' + shlex.join(cmd) + '\n')
            commands.flush()
            started = issued_mono
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
                   'issued_monotonic': issued_mono, 'issued_utc': issued_utc,
                   'stdout': (stdout or b'').decode(errors='replace'),
                   'stderr': (stderr or b'').decode(errors='replace'),
                   'cleanup': audit}
            commands.write('[%s exit=%s timeout=%s]\n' % (label, proc.returncode, timed_out))
            commands.flush()
            report['commands'].append({k: v for k, v in row.items() if k not in ('stdout', 'stderr')})
            ev['commands'] = report['commands']
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
            require(run('build-share-probe',
                        ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                         str(source), '-o', str(probe_bin)], timeout=60),
                    'probe compilation')
            ver = run('gcc-version', ['gcc', '--version'], timeout=15)
            report['compiler'] = ver['stdout'].splitlines()[0] if ver['exit_code'] == 0 else 'unknown'
            interp = run('probe-static-check', ['readelf', '-l', str(probe_bin)], timeout=15)
            require(interp, 'probe static inspection')
            if re.search(r'\bINTERP\b', interp['stdout']):
                raise RuntimeError('probe is not a static binary')
            selftest = run('probe-selftest', [str(probe_bin), 'selftest'], timeout=15)
            require(selftest, 'probe selftest')
            parsed = json.loads(selftest['stdout'])
            if parsed.get('status') != 'pass' or parsed.get('parse_weight') != 'pass':
                raise RuntimeError('probe selftest reported failure: ' + selftest['stdout'])

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
            row = cli('image-import', 'import', '-', IMAGE,
                      stdin=bundle.getvalue(), timeout=120)
            require(row, 'image import')
            report['image_id'] = row['stdout'].strip()

        def parse_inspect(text):
            parts = text.rstrip('\n').split('|')
            if len(parts) < 7:
                raise ValueError('incomplete docker inspect output: ' + text.strip())
            return {'shares': int(parts[0]), 'cpuset': parts[1],
                    'quota': int(parts[2]), 'period': int(parts[3]),
                    'id': parts[4], 'running': parts[5] == 'true',
                    'exit_code': int(parts[6]), 'raw': text.strip()}

        def wait_running(c, deadline_s=12.0):
            until = time.monotonic() + deadline_s
            attempt = 0
            while time.monotonic() < until:
                r = cli('poll-%s-%d' % (c, attempt), 'inspect', '-f',
                        '{{.State.Running}} {{.Id}}', NAMES[c], timeout=20)
                attempt += 1
                if r['exit_code'] == 0:
                    bits = r['stdout'].split()
                    if len(bits) == 2 and bits[0] == 'true':
                        return bits[1]
                time.sleep(0.2)
            raise RuntimeError(NAMES[c] + ' did not reach running state')

        def snapshot_of(c, label):
            row = checked_cli(label, 'exec', NAMES[c], PROBE_PATH, 'snapshot', timeout=25)
            events = checks.parse_events(row['stdout'])
            if len(events) != 1 or events[0].get('event') != 'snapshot':
                raise RuntimeError('%s produced %d events' % (label, len(events)))
            return events[0]

        def inspect_of(c, stage, label):
            row = checked_cli(label, 'inspect', '-f', INSPECT_TEMPLATE, NAMES[c], timeout=25)
            parsed = parse_inspect(row['stdout'])
            parsed['stage'] = stage
            return parsed

        def start_containers():
            spin = [PROBE_PATH, 'contend', '--threads', str(a.threads),
                    '--duration-ms', str(duration_ms), '--sample-ms', str(a.sample_ms)]
            for i, c in enumerate(CONTAINERS):
                if i:
                    time.sleep(0.15)  # deliberate stagger; the checker intersects it out
                run('prerm-' + c, base + ['docker', 'rm', '-f', NAMES[c]], timeout=20)
                flags = ['--cpuset-cpus', str(a.shared_cpu),
                         '--cpu-shares', str(PLAN[0][c])]
                row = cli('start-' + c, 'run', '-d', '--name', NAMES[c],
                          *flags, IMAGE, *spin, timeout=40)
                require(row, 'start ' + NAMES[c])
                cdata[c]['created_id'] = row['stdout'].strip()
                if wait_running(c) != cdata[c]['created_id']:
                    raise RuntimeError(NAMES[c] + ' identity changed during startup')

        def apply_boundary(stage):
            # Explicit pre/post guest-time brackets around every changed
            # container. The continuous in-container weight timeline remains
            # the scoring authority; these brackets are corroboration.
            for c in CONTAINERS:
                old, new = PLAN[stage - 1][c], PLAN[stage][c]
                if old == new:
                    continue
                pre = snapshot_of(c, 'preupd-%s-%d' % (c, stage))
                row = checked_cli('update-%s-%d' % (c, stage),
                                  'update', '--cpu-shares', str(new), NAMES[c], timeout=25)
                post = snapshot_of(c, 'postupd-%s-%d' % (c, stage))
                ev['updates'].append(
                    {'label': 'update-%s-%d' % (c, stage), 'stage': stage, 'container': c,
                     'from_shares': old, 'to_shares': new,
                     'exit_code': row['exit_code'], 'timed_out': row['timed_out'],
                     'issued_utc': row['issued_utc'], 'issued_monotonic': row['issued_monotonic'],
                     'pre': pre, 'post': post})
            for c in CONTAINERS:
                cdata[c]['snapshots'].append(
                    snapshot_of(c, 'snapshot-%s-%d' % (c, stage)))
                cdata[c]['inspections'].append(
                    inspect_of(c, stage, 'inspect-%s-%d' % (c, stage)))

        try:
            with (work / 'kernel-build.lock').open('a') as build_lock:
                fcntl.flock(build_lock, fcntl.LOCK_EX)
                build_probe()
            inputs_before = hash_inputs()
            lock_f = (work / 'kernel-build.lock').open('a')
            fcntl.flock(lock_f, fcntl.LOCK_SH)
            start_row = run('instance-start', base + ['start',
                                                       '--kernel', str(a.kernel.resolve()),
                                                       '--disk', str(a.disk.resolve()),
                                                       '--cpus', str(a.cpus)], timeout=180)
            require(start_row, 'instance start')
            info_row = cli('daemon-info', 'info', '--format', INFO_TEMPLATE, timeout=30)
            require(info_row, 'docker info')
            report['daemon_info'] = info_row['stdout'].strip()
            m = re.search(r'ncpu=(\d+)', report['daemon_info'])
            cg = re.search(r'cgroup=(\S+)', report['daemon_info'])
            sv = re.search(r'server=(\S+)', report['daemon_info'])
            if not m:
                raise RuntimeError('cannot parse daemon NCPU')
            ev['daemon'] = {'ncpu': int(m.group(1)),
                            'cgroup_version': cg.group(1) if cg else None,
                            'server_version': sv.group(1) if sv else None,
                            'info_raw': report['daemon_info']}
            import_image()
            start_containers()

            for c in CONTAINERS:
                cdata[c]['snapshots'].append(snapshot_of(c, 'snapshot-%s-0' % c))
                cdata[c]['inspections'].append(inspect_of(c, 0, 'inspect-%s-0' % c))
            schedule = {'baseline_done_monotonic': time.monotonic(),
                        'anchors_monotonic': [], 'boundaries': []}
            t0 = schedule['baseline_done_monotonic']
            for stage in (1, 2):
                anchor = t0 + stage * a.phase_ms / 1000.0
                while time.monotonic() < anchor:
                    time.sleep(min(0.05, max(0.0, anchor - time.monotonic())))
                schedule['anchors_monotonic'].append(time.monotonic())
                apply_boundary(stage)
                schedule['boundaries'].append(
                    {'stage': stage, 'at_monotonic': time.monotonic()})
            report['schedule'] = schedule

            for c in CONTAINERS:
                waited = checked_cli('wait-' + c, 'wait', NAMES[c],
                                     timeout=duration_ms / 1000 + 45)
                cdata[c]['wait_output'] = waited['stdout'].strip()
                final = parse_inspect(checked_cli('final-' + c, 'inspect', '-f',
                                                  INSPECT_TEMPLATE, NAMES[c], timeout=25)['stdout'])
                final['stage'] = checks.STAGES
                cdata[c]['inspections'].append(final)
                logs = checked_cli('logs-' + c, 'logs', NAMES[c], timeout=30)
                try:
                    cdata[c]['spin_events'] = checks.parse_events(logs['stdout'])
                except Exception as error:
                    cdata[c]['spin_error'] = '%s: %s' % (type(error).__name__, error)
                checked_cli('rm-' + c, 'rm', NAMES[c], timeout=30)
                cdata[c]['wait_exit'] = waited['exit_code']
                cdata[c]['probe_exit'] = cdata[c]['wait_output']
                cdata[c]['removed'] = True
            leftovers = checked_cli('ps-leftovers', 'ps', '-aq',
                                    '--filter', 'name=' + NAMES['a'],
                                    '--filter', 'name=' + NAMES['b'], timeout=20)
            ev['leftovers_stdout'] = leftovers['stdout']

            stop_row = run('instance-stop', base + ['stop'], timeout=90)
            report['stop_exit'] = stop_row['exit_code']
        except Exception as error:
            note_error('run', error)
        finally:
            try:
                state = harddind.read_state(instance)
                if harddind.live(state):
                    if state.get('phase') == 'ready':
                        for c in CONTAINERS:
                            run('cleanup-' + c, base + ['docker', 'rm', '-f', NAMES[c]], timeout=20)
                    run('final-stop', base + ['stop'], timeout=90)
                report['final_state'] = harddind.read_state(instance)
            except Exception as error:
                note_error('cleanup', error)
            ev['final_state'] = report.get('final_state', {})
            saved = out / 'instance'
            saved.mkdir(exist_ok=True)
            if instance.exists():
                for f in instance.iterdir():
                    if f.is_file() and f.suffix in ('.json', '.log'):
                        shutil.copy2(f, saved / f.name)
            before_rows = locals().get('inputs_before')
            if not before_rows:
                # Build failed before the binary existed: record everything
                # hashable, missing roles must fail closed in the checker.
                before_rows = hash_inputs()
            after_rows = hash_inputs()
            before_by_role = {r['role']: r['sha256'] for r in before_rows}
            after_by_role = {r['role']: r['sha256'] for r in after_rows}
            paths = {r['role']: r['path'] for r in after_rows}
            input_records = []
            for role in checks.INPUT_ROLES:
                input_records.append(
                    {'role': role, 'path': paths.get(role, ''),
                     'sha256_before': before_by_role.get(role),
                     'sha256_after': after_by_role.get(role)})
            ev['inputs'] = input_records
            report['sha256_inputs'] = input_records
            report['host_after'] = host_state()
            report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for c in CONTAINERS:
                cdata[c].setdefault('removed', False)
                ev['containers'][c] = cdata[c]

            try:
                report['checks'] = checks.evaluate(ev)
            except Exception as error:
                note_error('evaluate', error)
                report['checks'] = {}
            verdict = checks.overall_verdict(report['checks'], report['errors'])
            report['verdict'] = verdict
            report['runtime_verification'] = verdict
            (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            json.loads((out / 'report.json').read_text())  # serialization guard

    print(json.dumps({'experiment': a.name, 'verdict': report['verdict'],
                      'checks': {k: v.get('status') for k, v in report['checks'].items()},
                      'result': str(out / 'report.json'),
                      'errors': report['errors']}, indent=2))
    if report['verdict'] == checks.PASS:
        return 0
    return 1 if report['verdict'] == checks.FAIL else 2


if __name__ == '__main__':
    raise SystemExit(main())
