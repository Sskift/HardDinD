#!/usr/bin/env python3
"""run-cpu-witness - owner-run-ready round12 diagnostic harness.

Pattern proven by cpu-observer-owner-v1/supervisor.py, parameterized and
extended to run the v3 witness concurrently with the host-side correlator:

  python3 run-cpu-witness.py WORK NAME \
      [--kernel K] [--disk D] [--repo R] [--count 60] [--interval-ms 100]
      [--busy-duration-ms 45000] [--affinity 0,1,2,3] [--cpus 4]
      [--sampler-duration-s 30]

Four cells (strict completion, witness summary must report
requested==completed==count status=complete and exit 0):
  quiet-quota     docker run --rm --cpus 0.5            witness (no load)
  quiet-unlimited docker run --rm                      witness (no load)
  separate        docker run --rm (unlimited) witness beside a DETACHED
                  0.5-quota 4-thread busy probe in another cgroup
  same            docker exec inside the busy cgroup   witness

Each cell brackets the witness with its OWN host_correlate sampler process
(own process group, finite --duration-s deadline): started before the witness,
stopped after it by an explicit, planned SIGTERM with bounded wait/reap;
kill+reap fallback; analyze requires strict schema/count/status and bracketed
clock alignment. Sampler own CPU time/elapsed/sample counts are measured and
recorded (diagnostic overhead is stated, never called negligible).

Lock protocol: witness compilation takes EX on the global lock in a separate
phase; the guest lifecycle runs entirely under SH (never nested). The whole
harness inherits an explicit <=4-core host affinity. No GPU, no performance
claims; existing evidence paths cause failure; nothing is written into
cpu-observer-owner-v1 (its binary/scripts are read-only inputs).
"""
import argparse
import datetime
import fcntl
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback

HERE = pathlib.Path(__file__).resolve().parent
GLOBAL_LOCK = pathlib.Path('/root/harddind-rebuild/kernel-build.lock')
FROZEN_OBSERVER = pathlib.Path('/root/harddind-rebuild/cpu-observer-owner-v1')
FROZEN_PROBE = FROZEN_OBSERVER / 'cpu-limit-probe'
FROZEN_PROBE_SHA = 'afef800dc5c107087c896d444d12461865415563cc8e41d06950c86c78d5adaa'
WITNESS_SRC = HERE / 'cpu_capture_witness.c'
CORRELATOR = HERE / 'host_correlate.py'
IMAGE = 'harddind-witness-v3:local'
WITNESS_PATH = '/cpu-capture-witness'
BUSY_PATH = '/cpu-limit-probe'
CELLS = ('quiet-quota', 'quiet-unlimited', 'separate-observer', 'same-observer')
CONTAINERS = ('w-quiet-quota', 'w-quiet-unlimited', 'w-separate',
             'observer-busy-separate', 'observer-busy-same')


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def host_state():
    out = {}
    for p in ('/proc/loadavg', '/sys/fs/cgroup/cpu.stat', '/sys/fs/cgroup/cpu.max',
              '/sys/fs/cgroup/cpu.pressure'):
        try:
            out[p] = pathlib.Path(p).read_text().strip()
        except OSError as e:
            out[p] = '<%s: %s>' % (type(e).__name__, e)
    return out


def parse_affinity(text):
    cpus = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            raise ValueError('empty affinity element')
        if '-' in part:
            a, b = part.split('-', 1)
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    if not cpus or len(cpus) > 4 or any(c < 0 for c in cpus) \
            or len(set(cpus)) != len(cpus):
        raise ValueError('affinity must be 1..4 unique non-negative CPUs')
    online = os.sched_getaffinity(0)
    if not set(cpus) <= online:
        raise ValueError('requested CPUs %s not online %s' % (cpus, sorted(online)))
    return sorted(cpus)


def read_proc_cpu_ticks(pid):
    try:
        data = pathlib.Path('/proc/%d/stat' % pid).read_text()
    except OSError:
        return None
    fields = data.rsplit(') ', 1)[1].split()
    return int(fields[11]), int(fields[12])      # utime, stime (0-based 13,14)


class Harness:
    def __init__(self, args):
        self.a = args
        self.work = pathlib.Path(args.work).resolve()
        self.name = args.name
        self.out = self.work / 'results' / self.name
        self.instance = self.work / 'instances' / self.name
        self.repo = pathlib.Path(args.repo).resolve()
        self.kernel = pathlib.Path(args.kernel).resolve()
        self.disk = pathlib.Path(args.disk).resolve()
        sys.path.insert(0, str(self.repo / 'scripts'))
        import harddind          # noqa: E402
        from guest_cleanup import cleanup_group  # noqa: E402
        self.harddind = harddind
        self.cleanup_group = cleanup_group
        self.base = [sys.executable, str(self.repo / 'scripts/harddind.py'),
                     str(self.work), self.name]
        self.binary = self.out / 'cpu-capture-witness'
        self.report = None
        self.clk_tck = os.sysconf('SC_CLK_TCK')

    # -- process groups with deadlines and full reap ------------------------

    def run(self, label, cmd, stdin=None, timeout=90, required=True):
        start = time.monotonic_ns()
        sp = self.out / (label + '.stdout')
        ep = self.out / (label + '.stderr')
        with sp.open('xb') as so, ep.open('xb') as se:
            try:
                p = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=so, stderr=se, start_new_session=True)
            except OSError as exc:
                row = {'label': label, 'command': cmd, 'pid': None,
                       'exit_code': 127, 'timed_out': False,
                       'begin_ns': start, 'end_ns': time.monotonic_ns(),
                       'cleanup': None, 'spawn_error': str(exc)}
                self.report['commands'].append(row)
                self.save()
                if required:
                    raise RuntimeError('command spawn failed: %s (%s)'
                                       % (label, exc))
                return sp.read_text(), ep.read_text(), row
            timed_out = False
            try:
                p.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(p.pid, signal.SIGKILL)
                p.communicate(timeout=15)
            finally:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.communicate(timeout=15)
                cleanup = self.cleanup_group(p.pid)
        row = {'label': label, 'command': cmd, 'pid': p.pid,
               'exit_code': p.returncode, 'timed_out': timed_out,
               'begin_ns': start, 'end_ns': time.monotonic_ns(), 'cleanup': cleanup}
        self.report['commands'].append(row)
        self.save()
        if required and (p.returncode or timed_out or not cleanup['clean_shutdown']):
            raise RuntimeError('command failed: %s rc=%s timeout=%s'
                               % (label, p.returncode, timed_out))
        return sp.read_text(), ep.read_text(), row

    def cli(self, label, *args, **kw):
        return self.run(label, self.base + ['docker', *args], **kw)

    def save(self):
        (self.out / 'report.json').write_text(
            json.dumps(self.report, indent=2) + '\n')

    # -- phases --------------------------------------------------------------

    @staticmethod
    def acquire_lock(lock_f, shared, deadline_s=180.0):
        """Finite non-blocking flock acquisition; EX and SH are never nested."""
        flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + deadline_s
        while True:
            try:
                fcntl.flock(lock_f, flag | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('could not acquire %s lock within %ss'
                                       % ('SH' if shared else 'EX', deadline_s))
                time.sleep(0.2)

    def unique_label(self, base):
        """Distinct evidence label per invocation (run() opens files xb)."""
        counts = self.report.setdefault('_label_counts', {})
        n = counts.get(base, 0)
        counts[base] = n + 1
        return base if n == 0 else '%s-%d' % (base, n)

    def compile_witness(self):
        """Separate EX phase. Build and smoke both go through the recorded,
        exact-process-group bounded runner, so rc/timed_out/cleanup rows exist
        even on failure. EX is released before any SH is taken."""
        start = time.monotonic_ns()
        with GLOBAL_LOCK.open('a') as lock_f:
            self.acquire_lock(lock_f, shared=False)
            try:
                self.run('build-witness',
                         ['gcc', '-O2', '-Wall', '-Wextra', '-Werror',
                          '-static', '-pthread', str(WITNESS_SRC),
                          '-o', str(self.binary)], timeout=60)
                self.run('build-witness-smoke',
                         [str(self.binary), 'witness', '--count', '1',
                          '--interval-ms', '100'], timeout=20)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
        self.report['build'] = {'elapsed_ns': time.monotonic_ns() - start,
                                'witness_sha256': sha(self.binary)}

    def validate_raw_witness(self, stdout_text, stderr_text):
        """Strict actual completion on the raw witness output."""
        caps, syncs, summaries = [], [], []
        for line in stdout_text.splitlines():
            ev = json.loads(line)
            if ev.get('event') == 'cap':
                caps.append(ev)
            elif ev.get('event') == 'sync':
                syncs.append(ev)
            else:
                raise RuntimeError('unexpected witness event: %r' % ev.get('event'))
        for line in stderr_text.splitlines():
            line = line.strip()
            if line:
                ev = json.loads(line)
                if ev.get('event') != 'summary':
                    raise RuntimeError('unexpected witness stderr event')
                summaries.append(ev)
        if len(summaries) != 1:
            raise RuntimeError('witness summary count=%d' % len(summaries))
        summ = summaries[0]
        if (summ.get('status') != 'complete'
                or summ.get('requested') != self.a.count
                or summ.get('completed') != self.a.count):
            raise RuntimeError('witness not strictly complete: %r' % summ)
        if len(caps) != self.a.count:
            raise RuntimeError('witness cap count=%d != %d'
                               % (len(caps), self.a.count))
        if [c.get('k') for c in caps] != list(range(self.a.count)):
            raise RuntimeError('witness cap sequence broken')
        tags = [s.get('tag') for s in syncs]
        if sorted(tags) != ['end', 'start']:
            raise RuntimeError('witness sync tags wrong: %r' % tags)
        return caps, syncs, summ

    def run_cell(self, label, witness_cmd):
        """One witness invocation with its own sampler lifecycle. A cell
        record (including sampler pid/group) is created at sampler Popen and
        survives any later failure; every cleanup path records what it did.
        Host snapshots are taken per cell, immediately before launch."""
        state = self.harddind.read_state(self.instance)
        guest_pid = state.get('guest_pid')
        if not guest_pid or not pathlib.Path('/proc/%d' % guest_pid).is_dir():
            raise RuntimeError('guest pid unavailable for cell %s' % label)
        celldir = self.out / label
        celldir.mkdir(parents=True, exist_ok=True)
        sampledir = celldir / 'host'

        cell = self.report['cells'].setdefault(label, {
            'guest_pid': guest_pid, 'host_before': host_state(),
            'host_after': None, 'status': 'running',
            'sampler': None, 'witness': None, 'failure': None})

        sp_path = celldir / 'sampler.stdout'
        se_path = celldir / 'sampler.stderr'
        sp = sp_path.open('xb')
        se = se_path.open('xb')
        sampler_cmd = [sys.executable, str(CORRELATOR), 'sample',
                       '--uml-pid', str(guest_pid), '--out', str(sampledir),
                       '--duration-s', str(self.a.sampler_duration_s),
                       '--interval-ms', '10']
        sampler = subprocess.Popen(sampler_cmd, stdout=sp, stderr=se,
                                   start_new_session=True)
        sampler_begin = time.monotonic_ns()
        cpu_begin = read_proc_cpu_ticks(sampler.pid)
        # Record the resource immediately: pid, process group, start, and the
        # exact command survive even if every later step fails.
        try:
            pgid = os.getpgid(sampler.pid)
        except ProcessLookupError:
            pgid = None
        cell['sampler'] = {
            'pid': sampler.pid, 'pgid': pgid, 'command': sampler_cmd,
            'begin_ns': sampler_begin, 'end_ns': None, 'elapsed_s': None,
            'cpu_ticks_delta': None, 'cpu_s': None, 'samples': None,
            'termination': None, 'forced_kill': False, 'timed_out': False,
            'rc': None, 'planned_sigterm_ns': None, 'cleanup': None,
            'note': '10 ms diagnostic polling; overhead measured here, '
                    'never asserted negligible'}
        self.save()

        def finalize_sampler(planned, ticks_end):
            if cell['sampler'].get('end_ns') is not None:
                return
            end_ns = time.monotonic_ns()
            cell['sampler']['end_ns'] = end_ns
            cell['sampler']['elapsed_s'] = (end_ns - sampler_begin) / 1e9
            if cpu_begin and ticks_end:
                ticks = (ticks_end[0] - cpu_begin[0]) + \
                        (ticks_end[1] - cpu_begin[1])
                cell['sampler']['cpu_ticks_delta'] = max(0, ticks)
                cell['sampler']['cpu_s'] = max(0, ticks) / self.clk_tck
            cell['sampler']['termination'] = ('planned_sigterm' if planned
                                              else 'forced_cleanup')
            cell['sampler']['cleanup'] = self.cleanup_group(sampler.pid)

        try:
            # Wait until the sampler has written its first tick (bounded).
            deadline = time.monotonic() + 10
            samples_file = sampledir / 'samples.jsonl'
            while time.monotonic() < deadline:
                if samples_file.exists() and samples_file.stat().st_size > 0:
                    break
                if sampler.poll() is not None:
                    raise RuntimeError('sampler exited early rc=%s'
                                       % sampler.returncode)
                time.sleep(0.02)
            else:
                raise RuntimeError('sampler produced no samples within 10 s')

            stdout_text, stderr_text, cmdrow = self.run(
                label, witness_cmd, timeout=self.a.cell_timeout_s)

            caps, syncs, summ = self.validate_raw_witness(stdout_text,
                                                          stderr_text)

            # Explicit planned SIGTERM; bounded wait and full reap. Read the
            # sampler's own CPU ticks while the process still exists.
            cell['sampler']['planned_sigterm_ns'] = time.monotonic_ns()
            ticks_end = read_proc_cpu_ticks(sampler.pid)
            os.killpg(sampler.pid, signal.SIGTERM)
            try:
                sampler_rc = sampler.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(sampler.pid, signal.SIGKILL)
                cell['sampler']['forced_kill'] = True
                cell['sampler']['timed_out'] = True
                sampler_rc = sampler.wait(timeout=15)
            finalize_sampler(planned=True, ticks_end=ticks_end)
            sp.close(); se.close()
            if sampler_rc != 0:
                raise RuntimeError('sampler planned stop rc=%s' % sampler_rc)
            cleanup = cell['sampler']['cleanup']
            if not cleanup or not cleanup.get('clean_shutdown'):
                raise RuntimeError('sampler cleanup not clean: %r' % cleanup)
            sample_summary = json.loads(
                sp_path.read_text().strip().splitlines()[-1])
            cell['sampler']['samples'] = sample_summary.get('samples')
            cell['sampler']['termination_reason'] = \
                sample_summary.get('termination_reason')
            cell['sampler']['rc'] = sampler_rc
            if not sample_summary.get('ok') \
                    or sample_summary.get('termination_reason') != 'signal':
                raise RuntimeError('sampler termination abnormal: %r'
                                   % sample_summary)

            (celldir / 'witness.stdout').write_text(stdout_text)
            (celldir / 'witness.stderr').write_text(stderr_text)

            corr_out = celldir / 'corr'
            self.run(label + '-analyze',
                     [sys.executable, str(CORRELATOR), 'analyze',
                      '--samples', str(sampledir),
                      '--witness', str(celldir / 'witness.stdout'),
                      '--summary', str(celldir / 'witness.stderr'),
                      '--expect-count', str(self.a.count),
                      '--out', str(corr_out)], timeout=30)
            correlation = json.loads((corr_out / 'correlation.json').read_text())
            if not correlation.get('attribution_valid'):
                raise RuntimeError('correlation attribution invalid for %s: %r'
                                   % (label, correlation['schema']['problems']))

            cell.update({
                'status': 'complete',
                'witness': {'requested': summ['requested'],
                            'completed': summ['completed'],
                            'status': summ['status'],
                            'sync_tags': [s['tag'] for s in syncs]},
                'coverage': correlation['coverage_counts'],
                'alignment_width_ns':
                    correlation['clock_alignment'].get('width_ns'),
                'host_after': host_state(),
                'command': cmdrow,
            })
            self.save()
        except BaseException as exc:
            # Deterministic failure record; never leak the sampler. A green
            # cleanup claim requires the actual process group audit below.
            cell['status'] = 'failed'
            cell['failure'] = '%s: %s' % (type(exc).__name__, exc)
            ticks_end = read_proc_cpu_ticks(sampler.pid)
            try:
                if sampler.poll() is None:
                    os.killpg(sampler.pid, signal.SIGKILL)
                    cell['sampler']['forced_kill'] = True
                    try:
                        sampler.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        cell['sampler']['timed_out'] = True
            finally:
                finalize_sampler(planned=False, ticks_end=ticks_end)
                try:
                    sp.close(); se.close()
                except Exception:
                    pass
                cell['host_after'] = cell['host_after'] or host_state()
                self.save()
            raise

    def busy_state(self, container):
        txt, _, _ = self.run(
            self.unique_label('busy-inspect-' + container),
            self.base + ['docker', 'inspect', '-f',
                         '{{.State.Running}}|{{.State.StartedAt}}|'
                         '{{.State.FinishedAt}}|{{.State.ExitCode}}',
                         container], timeout=20)
        parts = txt.strip().split('|')
        return {'running': parts[0] == 'true', 'started_at': parts[1],
                'finished_at': parts[2], 'exit_code': parts[3] if len(parts) > 3 else None,
                'raw': txt.strip()}

    def start_busy(self, container):
        """Fresh independent 4-thread 0.5-quota load for one observer cell."""
        self.run('busy-prerm-' + container,
                 self.base + ['docker', 'rm', '-f', container],
                 timeout=20, required=False)
        self.run('busy-start-' + container,
                 self.base + ['docker', 'run', '-d', '--name', container,
                             '--cpus', '0.5', IMAGE, BUSY_PATH, 'spin',
                             '--threads', '4',
                             '--duration-ms', str(self.a.busy_duration_ms),
                             '--sample-ms', '100'], timeout=40)
        deadline = time.monotonic() + 12
        before = None
        while time.monotonic() < deadline:
            before = self.busy_state(container)
            if before['running']:
                break
            time.sleep(0.2)
        if not before or not before['running']:
            raise RuntimeError('busy load %s never running' % container)
        return before

    def finish_busy(self, container, before, cell_label):
        """Require the load to have survived the whole cell, then collect."""
        after = self.busy_state(container)
        if not after['running']:
            raise RuntimeError(
                'busy load %s vanished during cell %s (started %s finished %s); '
                'no same/separate comparison is valid for this cell'
                % (container, cell_label, before['started_at'],
                   after['finished_at']))
        wait_txt, _, _ = self.run('busy-wait-' + container,
                                  self.base + ['docker', 'wait', container],
                                  timeout=self.a.busy_duration_ms / 1000 + 60)
        if wait_txt.strip() != '0':
            raise RuntimeError('busy probe %s exit=%r' % (container,
                                                         wait_txt.strip()))
        self.run('busy-logs-' + container,
                 self.base + ['docker', 'logs', container], timeout=30)
        final = self.busy_state(container)
        self.run('busy-remove-' + container,
                 self.base + ['docker', 'rm', container], timeout=30)
        return {'before': before, 'after_alive': after, 'final': final}

    def run_busy_cell(self, cell_label, container, observer_cmd):
        """start independent load -> observer cell -> prove load survived."""
        before = self.start_busy(container)
        try:
            self.run_cell(cell_label, observer_cmd)
        except BaseException:
            self.run('busy-cleanup-' + container,
                     self.base + ['docker', 'rm', '-f', container],
                     timeout=20, required=False)
            raise
        result = self.finish_busy(container, before, cell_label)
        self.report['cells'][cell_label]['busy_load'] = result
        self.save()

    def main(self):
        out, instance = self.out, self.instance
        if out.exists() or instance.exists():
            raise SystemExit('refusing existing evidence: %s or %s'
                             % (out, instance))
        out.mkdir(parents=True, exist_ok=True)
        cpus = parse_affinity(self.a.affinity)
        os.sched_setaffinity(0, cpus)
        inputs = [pathlib.Path(__file__).resolve(), WITNESS_SRC, CORRELATOR,
                  FROZEN_PROBE, self.kernel, self.disk,
                  self.repo / 'scripts/harddind.py',
                  self.repo / 'scripts/guest_cleanup.py',
                  self.repo / 'scripts/port_driver.py',
                  self.repo / 'scripts/result_checks.py']
        self.report = {
            'status': 'running', 'pid': os.getpid(),
            'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'kind': 'cpu_witness_placement_diagnostic_v3',
            'experiment': self.name, 'work': str(self.work),
            'full_acceptance': False, 'performance_acceptance': False,
            'params': {'count': self.a.count,
                       'interval_ms': self.a.interval_ms,
                       'busy_duration_ms': self.a.busy_duration_ms,
                       'sampler_duration_s': self.a.sampler_duration_s,
                       'sampler_interval_ms': 10,
                       'guest_cpus': self.a.cpus,
                       'host_affinity_cpus': cpus},
            'frozen_inputs': {'busy_probe': str(FROZEN_PROBE),
                              'busy_probe_sha256_expected': FROZEN_PROBE_SHA},
            'commands': [], 'cells': {},
            'sha256': {str(p): sha(p) for p in inputs if p.exists()},
            'limitations': [
                'diagnostic only; no causal attribution when alignment/coverage invalid',
                '10 ms host sampling is coarse; schedstat wait absence means unknown',
                'one sequential wave; intermittent rates not bounded',
                'sampler consumes host CPU; measured per cell, not negligible',
                'no performance or enforcement acceptance'],
        }
        if sha(FROZEN_PROBE) != FROZEN_PROBE_SHA:
            raise SystemExit('frozen busy probe hash mismatch; aborting')
        self.save()
        rc = 1
        lock_f = None
        try:
            self.compile_witness()
            if sha(self.binary) != self.report['build']['witness_sha256']:
                raise RuntimeError('witness binary changed post build')

            self.work.mkdir(parents=True, exist_ok=True)
            lock_link = self.work / 'kernel-build.lock'
            if not lock_link.exists():
                lock_link.symlink_to(GLOBAL_LOCK)
            lock_f = lock_link.open('a')
            # Separate, bounded SH acquisition (EX was fully released above).
            self.acquire_lock(lock_f, shared=True)

            self.run('instance-start',
                     self.base + ['start', '--kernel', str(self.kernel),
                                 '--disk', str(self.disk),
                                 '--cpus', str(self.a.cpus)], timeout=180)
            self.report['ready_state'] = self.harddind.read_state(instance)
            self.cli('daemon-info', 'info',
                     '--format', '{{.NCPU}} {{.ServerVersion}} '
                                  '{{.KernelVersion}}', timeout=30)

            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode='w') as archive:
                for directory in ('dev', 'proc', 'sys', 'tmp'):
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                for src, arc in ((self.binary, WITNESS_PATH.lstrip('/')),
                                (FROZEN_PROBE, BUSY_PATH.lstrip('/'))):
                    info = archive.gettarinfo(str(src), arcname=arc)
                    info.mode = 0o755
                    info.uname = info.gname = 'root'
                    with open(src, 'rb') as fh:
                        archive.addfile(info, fh)
            self.cli('image-import', 'import', '-', IMAGE,
                     stdin=bundle.getvalue(), timeout=120)

            witness_args = [WITNESS_PATH, 'witness', '--count',
                            str(self.a.count), '--interval-ms',
                            str(self.a.interval_ms)]
            # host_before is taken inside run_cell, per cell, immediately.

            self.run_cell('quiet-quota',
                          self.base + ['docker', 'run', '--rm', '--name',
                                      'w-quiet-quota', '--cpus', '0.5', IMAGE,
                                      *witness_args])
            self.run_cell('quiet-unlimited',
                          self.base + ['docker', 'run', '--rm', '--name',
                                      'w-quiet-unlimited', IMAGE,
                                      *witness_args])

            # Independent fresh busy load per observer cell, so a fixed busy
            # duration can never expire between the two observer runs.
            self.run_busy_cell(
                'separate-observer', 'observer-busy-separate',
                self.base + ['docker', 'run', '--rm', '--name', 'w-separate',
                             IMAGE, *witness_args])
            self.run_busy_cell(
                'same-observer', 'observer-busy-same',
                self.base + ['docker', 'exec', 'observer-busy-same',
                             *witness_args])
            inv, _, _ = self.run('inventory',
                                self.base + ['docker', 'ps', '-aq'], timeout=20)
            if inv.strip():
                raise RuntimeError('residual containers: %r' % inv.strip())
            self.run('instance-stop', self.base + ['stop'], timeout=90)
            rc = 0
        except BaseException:
            self.report['error'] = traceback.format_exc()
        finally:
            try:
                state = self.harddind.read_state(instance)
                if self.harddind.live(state) and state.get('phase') == 'ready':
                    for name in CONTAINERS:
                        self.run('cleanup-' + name,
                                 self.base + ['docker', 'rm', '-f', name],
                                 timeout=20, required=False)
                if self.harddind.live(self.harddind.read_state(instance)):
                    self.run('final-stop', self.base + ['stop'],
                             timeout=90, required=False)
                state = self.harddind.read_state(instance)
                self.report['final_state'] = state
                self.report['normal_shutdown'] = (
                    state.get('phase') == 'stopped'
                    and not state.get('forced_shutdown')
                    and state.get('guest_cleanup', {}).get('clean_shutdown')
                    and state.get('network_cleanup', {}).get('clean_shutdown'))
                post_hashes = {str(p): sha(p) for p in
                               [pathlib.Path(k) for k in self.report['sha256']]
                               if p.exists()}
                self.report['inputs_unchanged'] = (
                    post_hashes == self.report['sha256'])
                if self.binary.exists():
                    bh = sha(self.binary)
                    self.report['witness_binary_unchanged'] = (
                        bh == self.report.get('build', {}).get('witness_sha256'))
                self.report['host_after'] = host_state()
                if lock_f is not None:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
                if not self.report['normal_shutdown'] \
                        or not self.report['inputs_unchanged'] \
                        or not self.report.get('witness_binary_unchanged', True):
                    rc = 1
            except BaseException:
                self.report['cleanup_error'] = traceback.format_exc()
                rc = 1
            self.report.update(
                status='finished', exit_code=rc,
                finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            self.save()
        return rc

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', help='work root (results/ and instances/ created here)')
    p.add_argument('name', help='immutable experiment name; existing evidence fails')
    p.add_argument('--kernel', default='/root/harddind-rebuild/cpu-cpuset-kernel-v1')
    p.add_argument('--disk', default='/root/harddind-rebuild/runtime-shares-v1.ext4')
    p.add_argument('--repo',
                   default='/root/harddind-rebuild/cpu-observer-owner-v1/repo')
    p.add_argument('--count', type=int, default=60)
    p.add_argument('--interval-ms', type=int, default=100)
    p.add_argument('--busy-duration-ms', type=int, default=45000)
    p.add_argument('--sampler-duration-s', type=float, default=30.0)
    p.add_argument('--cell-timeout-s', type=float, default=40.0)
    p.add_argument('--affinity', default='0,1,2,3',
                   help='1..4 host CPU ids, e.g. 0,1 or 0-3')
    p.add_argument('--cpus', type=int, default=4)
    a = p.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,39}', a.name):
        raise SystemExit('bad name')
    if not (1 <= a.count <= 2000 and 20 <= a.interval_ms <= 5000):
        raise SystemExit('bad count/interval')
    if not (5000 <= a.busy_duration_ms <= 120000
            and 1.0 <= a.sampler_duration_s <= 120.0
            and 1.0 <= a.cell_timeout_s <= 120.0 and 1 <= a.cpus <= 64):
        raise SystemExit('bad duration arguments')
    harness = Harness(a)
    raise SystemExit(harness.main())


if __name__ == '__main__':
    main()
