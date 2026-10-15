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
import raw_output_index
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


def _json_default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode(errors='replace')
    return repr(obj)


def host_state():
    state = {'nproc': os.cpu_count()}
    for label, path in HOST_READ_FILES:
        try:
            state[label] = pathlib.Path(path).read_text().strip()
        except OSError as error:
            state[label] = '<%s: %s>' % (type(error).__name__, error)
    return state


def _read_only_run_summary(src_dir, by_label):
    """Diagnostic-only raw durations/run epochs from archived probe logs.
    Never a verdict: reported only when complete command metadata exists elsewhere.
    Purely arithmetic over the recorded JSONL; no inference of enforcement."""
    out = {}
    for c in ('a', 'b'):
        f = src_dir / ('logs-%s.stdout' % c)
        if not f.exists():
            continue
        events = []
        for line in f.read_text(errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and e.get('event') == 'sample':
                events.append(e)
        if len(events) < 2:
            continue
        gaps = [(events[i + 1]['wall_ns'] - events[i].get('cap_end_ns', events[i]['wall_ns'])) / 1e9
                for i in range(len(events) - 1)]
        big = [round(g, 3) for g in gaps if g > 0.5]
        # Weight runs from observed bracket weights (stable fast captures only).
        runs, w = [], events[0].get('weight1')
        t0 = events[0]['wall_ns']
        last_end = events[0].get('cap_end_ns', events[0]['wall_ns'])
        n = 0
        for e in events[1:]:
            ww = e.get('weight1')
            if ww != w:
                runs.append({'weight': w, 'start_s': round(t0 / 1e9, 3),
                            'end_s': round(last_end / 1e9, 3), 'samples': n})
                w, t0, n = ww, e['wall_ns'], 0
            n += 1
            last_end = e.get('cap_end_ns', e['wall_ns'])
        runs.append({'weight': w, 'start_s': round(t0 / 1e9, 3),
                    'end_s': round(last_end / 1e9, 3), 'samples': n})
        out[c] = {'samples': len(events),
                   'max_sample_gap_s': round(max(gaps), 3),
                   'gaps_over_0.5_s': big, 'weight_runs': runs}
    return out


def index_only_main(src_dir, out_dir):
    """Production post-hoc indexing of an existing (possibly immutable,
    crashed) results directory via the SAME raw_output_index code used during
    live finalization. Never mutates src_dir; writes products to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    indexed = raw_output_index.index_results_dir(src_dir)
    (out_dir / 'raw-output-index.json').write_text(
        json.dumps(indexed, indent=2) + '\n')
    by_label = {r['label']: r for r in indexed['index']}
    raw_summary = _read_only_run_summary(src_dir, by_label)
    has_report = (src_dir / 'report.json').exists()
    complete = (indexed['labels_in_commands_log'] > 0
                and indexed['integrity_ok'] and has_report)
    recovered = {'kind': 'cpu_share_raw_output_index',
                 'source_results_dir': str(src_dir),
                 'integrity_ok': indexed['integrity_ok'],
                 'labels_indexed': indexed['labels_indexed'],
                 'labels_in_commands_log': indexed['labels_in_commands_log'],
                 'errors': indexed['errors'],
                 'report_present_in_source': has_report,
                 # When the original run crashed before persisting metadata,
                 # commands.log lacks PID/cleanup rows: the index is a
                 # diagnostic recovery, never a functional verdict.
                 'functional_verdict': None,
                 'complete_command_metadata': complete,
                 'raw_diagnostic_summary': raw_summary,
                 'raw_diagnostic_note': (
                     'read-only durations/weight-run summary from archived probe '
                     'JSONL; NOT a functional verdict; requires a fresh owner '
                     'run for any pass') if not complete else
                 'full report present; raw summary supplementary'}
    (out_dir / 'index-report.json').write_text(
        json.dumps(recovered, indent=2) + '\n')
    print(json.dumps({k: recovered[k] for k in (
        'integrity_ok', 'labels_indexed', 'labels_in_commands_log',
        'report_present_in_source', 'complete_command_metadata',
        'functional_verdict', 'raw_diagnostic_summary',
        'raw_diagnostic_note')}, indent=2, default=_json_default))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', nargs='?', type=pathlib.Path)
    p.add_argument('--index-only', dest='index_only', type=pathlib.Path, default=None,
                   help='run the production raw-output indexer over an existing '
                        'results directory (read-only there); needs --index-out')
    p.add_argument('--index-out', dest='index_out', type=pathlib.Path, default=None)
    p.add_argument('--kernel', type=pathlib.Path)
    p.add_argument('--disk', type=pathlib.Path)
    p.add_argument('--name', help='immutable experiment name; reuse is refused')
    p.add_argument('--cpus', type=int, default=4, help='guest vCPU count (protocol requires 4)')
    p.add_argument('--shared-cpu', type=int, default=2, help='guest CPU both containers contend for')
    p.add_argument('--threads', type=int, default=4, help='busy threads per container (2..16)')
    p.add_argument('--phase-ms', type=int, default=9000, help='stage spacing (5000..20000)')
    p.add_argument('--sample-ms', type=int, default=100, help='probe sample interval (50..500)')
    p.add_argument('--stable-window-ms', type=int, default=4500,
                   help='bounded busy observation after final weight confirmed (>=4000)')
    p.add_argument('--overhead-budget-ms', type=int, default=15000,
                   help='declared budget for sequential command overhead (5000..30000)')
    a = p.parse_args()
    if a.index_only is not None:
        if a.index_out is None:
            p.error('--index-only requires --index-out')
        if not a.index_only.is_dir():
            p.error('--index-only directory does not exist: %s' % a.index_only)
        return index_only_main(a.index_only.resolve(), a.index_out.resolve())
    if a.work is None or a.kernel is None or a.disk is None or a.name is None:
        p.error('work, --kernel, --disk and --name are required for a live run')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,27}', a.name):
        p.error('name must match [a-z0-9][a-z0-9_-]{0,27}')
    if a.cpus != 4:
        p.error('this protocol requires a 4-vCPU guest (got %d)' % a.cpus)
    if not 0 <= a.shared_cpu < a.cpus:
        p.error('shared cpu must be in 0..%d' % (a.cpus - 1))
    if not 2 <= a.threads <= 16 or not 5000 <= a.phase_ms <= 20000 or not 50 <= a.sample_ms <= 500:
        p.error('invalid thread/phase/sample parameters')
    if not 4000 <= a.stable_window_ms <= 10000:
        p.error('stable window must be 4000..10000 ms (required phase floor is 4 s)')
    if not 5000 <= a.overhead_budget_ms <= 30000:
        p.error('overhead budget must be 5000..30000 ms')
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
    # Hard operational ceiling for each probe: head + all anchors + the bounded
    # final stable window + a declared sequential-command overhead budget.
    # Graceful SIGTERM ends the workload earlier, once the final weight has been
    # observed on both containers for the full stable window. If real overhead
    # exceeds the budget and the ceiling fires first, the missing final window
    # fails rather than being silently squeezed.
    duration_ms = (HEAD_MS + 3 * a.phase_ms + a.stable_window_ms
                   + a.overhead_budget_ms)
    if duration_ms > 120000:
        p.error('contend hard ceiling would exceed probe 120 s limit')
    started_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        'kind': 'docker_cpu_share_contention',
        'schema_version': 4,
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
                   'stable_window_ms': a.stable_window_ms,
                   'overhead_budget_ms': a.overhead_budget_ms,
                   'completion': 'graceful_sigterm_after_final_stable_phase',
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

    # Incremental metadata sink: every command row is appended and fsynced
    # BEFORE the next command runs, so a later crash or finalization exception
    # can never lose all command metadata (the v5 failure mode). Created with
    # 'x' alongside commands.log so a rerun collision is still refused.
    meta_fp = (out / 'command-metadata.jsonl').open('x')

    def persist_meta(row):
        safe = {k: v for k, v in row.items() if k not in ('stdout', 'stderr')}
        meta_fp.write(json.dumps(safe) + '\n')
        meta_fp.flush()
        os.fsync(meta_fp.fileno())

    def persist_input_hashes(rows, name):
        f = out / name
        tmp = out / (name + '.tmp')
        tmp.write_text(json.dumps(rows, indent=2) + '\n')
        os.replace(tmp, f)
        os.sync()

    def persist_report_anyway(report, reason):
        """Last-resort writer used when normal finalization/serialization
        raises. Never raises: if even this fails, the JSONL metadata on disk
        remains. A report written here is always fail-closed."""
        try:
            fallback = {k: report.get(k) for k in (
                'kind', 'experiment', 'schema_version', 'started_at',
                'finished_at', 'params', 'errors', 'commands',
                'final_state', 'host_before')}
            fallback['verdict'] = 'fail'
            fallback['runtime_verification'] = 'fail'
            fallback['finalization_failure'] = reason
            fallback['checks'] = report.get('checks') or {}
            tmp = out / 'report.json.tmp'
            tmp.write_text(json.dumps(fallback, indent=2, default=_json_default) + '\n')
            os.replace(tmp, out / 'report.json')
            return True
        except Exception:
            return False

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
                # Persist the identity before waiting, including interrupted calls.
                with (out / 'command-launches.jsonl').open('a') as launches:
                    launches.write(json.dumps({'label': label, 'pid': proc.pid,
                                              'command': cmd,
                                              'issued_utc': issued_utc}) + '\n')
                    launches.flush()
                    os.fsync(launches.fileno())
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
            meta_row = {k: v for k, v in row.items() if k not in ('stdout', 'stderr')}
            report['commands'].append(meta_row)
            ev['commands'] = report['commands']
            persist_meta(row)   # durable before any subsequent work can fail
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
            # Bound all updates of one stage with explicit guest-time brackets.
            # Issue all pre-bracket reads first, then the updates promptly,
            # then all post-bracket reads: this bounds the sequential exec
            # overhead around a weight change instead of burying later
            # containers' updates behind earlier snapshots.
            changed = [c for c in CONTAINERS if PLAN[stage - 1][c] != PLAN[stage][c]]
            pres = {c: snapshot_of(c, 'preupd-%s-%d' % (c, stage)) for c in changed}
            rows = {}
            for c in changed:
                rows[c] = checked_cli('update-%s-%d' % (c, stage),
                                      'update', '--cpu-shares', str(PLAN[stage][c]),
                                      NAMES[c], timeout=25)
            posts = {c: snapshot_of(c, 'postupd-%s-%d' % (c, stage)) for c in changed}
            for c in changed:
                old, new = PLAN[stage - 1][c], PLAN[stage][c]
                ev['updates'].append(
                    {'label': 'update-%s-%d' % (c, stage), 'stage': stage, 'container': c,
                     'from_shares': old, 'to_shares': new,
                     'exit_code': rows[c]['exit_code'], 'timed_out': rows[c]['timed_out'],
                     'issued_utc': rows[c]['issued_utc'],
                     'issued_monotonic': rows[c]['issued_monotonic'],
                     'pre': pres[c], 'post': posts[c]})
            for c in CONTAINERS:
                cdata[c]['snapshots'].append(
                    snapshot_of(c, 'snapshot-%s-%d' % (c, stage)))
                cdata[c]['inspections'].append(
                    inspect_of(c, stage, 'inspect-%s-%d' % (c, stage)))

        def confirm_final_weights(stage_snaps):
            # The final stable phase is equal weight on both containers, and
            # B must actually have moved away from its middle weight. Uses
            # only observed in-guest values; no shares-derived fraction.
            w = {c: stage_snaps[c]['cgroup']['cpu.weight']['raw'] for c in CONTAINERS}
            if w['a'] != w['b']:
                raise RuntimeError('final stage weights differ: %r' % w)
            b_mid = cdata['b']['snapshots'][1]['cgroup']['cpu.weight']['raw']
            if w['b'] == b_mid:
                raise RuntimeError('B never reached its final weight (still %s)' % w['b'])
            return w

        def graceful_finish(weights):
            # Bounded explicit completion: after BOTH containers are observed
            # in the final stable weight, keep the busy workload alive for a
            # fixed stable window (>= the 4 s window floor), then issue
            # SIGTERM SEQUENTIALLY in a fixed order (a then b) with no other
            # docker work between them; each docker kill returns promptly.
            # Sequential issuance is recorded (stop-a then stop-b); no
            # cross-container simultaneity is claimed. Both probes flush a
            # graceful end event and exit 0 bounded; the hard duration ceiling
            # remains the operational fallback.
            window_start = time.monotonic()
            while time.monotonic() < window_start + a.stable_window_ms / 1000.0:
                time.sleep(0.05)
            stop = {}
            stop_order = []
            for c in CONTAINERS:
                stop[c] = cli('stop-' + c, 'kill', '--signal=TERM', NAMES[c], timeout=25)
                require(stop[c], 'graceful stop ' + NAMES[c])
                stop_order.append({'container': c, 'label': 'stop-' + c,
                                   'issued_monotonic': stop[c]['issued_monotonic'],
                                   'issued_utc': stop[c]['issued_utc']})
            stop_gap = None
            if len(stop_order) == 2:
                stop_gap = stop_order[1]['issued_monotonic'] - stop_order[0]['issued_monotonic']
            return {'stable_window_ms': a.stable_window_ms,
                    'window_start_monotonic': window_start,
                    'window_end_monotonic': time.monotonic(),
                    'final_weights': weights,
                    'stop_issuance': 'sequential_ordered_a_then_b',
                    'stop_order': stop_order,
                    'stop_issuance_gap_s': stop_gap,
                    'stop_issued': {c: {'exit_code': stop[c]['exit_code'],
                                        'issued_utc': stop[c]['issued_utc'],
                                        'issued_monotonic': stop[c]['issued_monotonic']}
                                    for c in CONTAINERS}}

        try:
            build_probe()
            inputs_before = hash_inputs()
            # Persist input hashes BEFORE the guest can start; a later crash
            # leaves provenance on disk.
            persist_input_hashes(inputs_before, 'input-hashes-before.json')
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
                        'anchors_monotonic': [], 'boundaries': [],
                        'completion': None}
            t0 = schedule['baseline_done_monotonic']
            for stage in (1, 2):
                # Anchors are earliest-start points; if a previous boundary
                # overran (sequential docker exec latency), proceed at once
                # rather than pile anchors into the final phase. The hard
                # ceiling + completion window bound the total run.
                anchor = t0 + stage * a.phase_ms / 1000.0
                while time.monotonic() < anchor:
                    time.sleep(min(0.05, max(0.0, anchor - time.monotonic())))
                schedule['anchors_monotonic'].append(time.monotonic())
                apply_boundary(stage)
                schedule['boundaries'].append(
                    {'stage': stage, 'at_monotonic': time.monotonic()})
            final_weights = confirm_final_weights(
                {c: cdata[c]['snapshots'][-1] for c in CONTAINERS})
            schedule['completion'] = graceful_finish(final_weights)
            report['schedule'] = schedule

            for c in CONTAINERS:
                # Graceful probes exit promptly; the timeout still covers the
                # hard-ceiling fallback (signal lost or legacy binary).
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
            persist_input_hashes(after_rows, 'input-hashes-after.json')
            report['host_after'] = host_state()
            report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for c in CONTAINERS:
                cdata[c].setdefault('removed', False)
                ev['containers'][c] = cdata[c]
            ev['schema_version'] = 4

            # Production raw-output indexing (type-aware). This is the same
            # code path the post-hoc --index-only tool uses: pretty JSON
            # objects, JSON scalars, text and strict probe JSONL are all
            # distinguished, so a non-object probe line can never be treated
            # as a successful record (v5 AttributeError root cause).
            indexed = raw_output_index.index_results_dir(out)
            (out / 'raw-output-index.json').write_text(
                json.dumps(indexed, indent=2) + '\n')
            ev['raw_replay'] = indexed['index']
            ev['raw_index_errors'] = indexed['errors']
            ev['raw_index_integrity_ok'] = indexed['integrity_ok']
            report['raw_output_index'] = {
                'labels_indexed': indexed['labels_indexed'],
                'labels_in_commands_log': indexed['labels_in_commands_log'],
                'integrity_ok': indexed['integrity_ok'],
                'errors': indexed['errors']}

            try:
                report['checks'] = checks.evaluate(ev)
            except Exception as error:
                note_error('evaluate', error)
                report['checks'] = {}
            # Diagnostic index errors do not disappear, but they are
            # fail-closed integrity: an expected probe record that did not
            # parse as a JSON object forces an overall FAIL.
            if not indexed['integrity_ok']:
                note_error('raw_output_index', RuntimeError(
                    'recorded command output failed strict index integrity: %r'
                    % indexed['errors'][:3]))
            verdict = checks.overall_verdict(report['checks'], report['errors'])
            report['verdict'] = verdict
            report['runtime_verification'] = verdict
            # Retain the COMPLETE evaluated evidence (fixes v3's empty
            # evidence bug); raw stdout/stderr remain on disk and are indexed
            # in ev['raw_replay']. Serialization is guarded: any exception
            # here writes a fail-closed report instead of leaving none.
            try:
                ev_native = json.loads(json.dumps(ev, default=_json_default))
                report['evidence'] = ev_native
                tmp = out / 'report.json.tmp'
                tmp.write_text(json.dumps(report, indent=2) + '\n')
                os.replace(tmp, out / 'report.json')
                reloaded = json.loads((out / 'report.json').read_text())
                assert reloaded.get('evidence') and reloaded['evidence'].get('containers')
                assert reloaded['evidence'].get('raw_replay') is not None
            except Exception as serr:
                note_error('finalize_serialization', serr)
                report['verdict'] = report['runtime_verification'] = 'fail'
                persisted = persist_report_anyway(report, '%s: %s' % (
                    type(serr).__name__, serr))
                if not persisted:
                    raise
            try:
                meta_fp.close()
            except Exception:
                pass

    summary = {'experiment': a.name, 'verdict': report.get('verdict'),
               'checks': {k: v.get('status') for k, v in report.get('checks', {}).items()},
               'result': str(out / 'report.json'),
               'errors': report.get('errors', []),
               'raw_index_integrity_ok': report.get('raw_output_index', {}).get('integrity_ok')}
    print(json.dumps(summary, indent=2))
    if report.get('verdict') == checks.PASS:
        return 0
    return 1 if report.get('verdict') == checks.FAIL else 2


if __name__ == '__main__':
    raise SystemExit(main())
