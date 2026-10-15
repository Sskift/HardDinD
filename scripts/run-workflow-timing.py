#!/usr/bin/env python3
"""End-to-end Docker workflow latency runner for the persistent HardDinD runtime.

The boolean docker-suite.sh proves commands work and run-core.py measures
in-guest microbenchmarks; neither reports the wall latency of *complete real
Docker workflows*. This runner drives the ordinary stock Docker CLI through
``scripts/harddind.py ... docker`` (one real CLI process per command) and
times representative workflows independently, one selectable shard per
invocation so a parent can keep each run behind a bounded supervisor:

* lifecycle: create/start/exec/wait/stop/restart/rm with identity and output
* image: local import, data-bearing commit, save/load roundtrip with image ID
* volume: named volume plus docker cp in/out roundtrip checked by digest
* build: fixed-local BuildKit cold (--no-cache) and cached rebuild, run digest
* net: user-network embedded-DNS lookup and container-to-container HTTP body

Every component keeps its raw stdout/stderr, exit status, monotonic duration
and per-command timeout. Failed, timed-out and never-run observations are all
retained and listed; nothing missing is reported as a pass. Setup (instance
boot, fixture import), every component and cleanup (object reap, instance
stop) are timed separately.

There is no usable native Docker endpoint or host socket in this
unprivileged container, so native comparison is always "not_tested" and
near-native/full acceptance is always false. Launching host processes is
never timed as a substitute for Docker latency. No performance threshold is
applied here; kernel A/B comparisons belong to analyze-workflow-timing.py.
"""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harddind  # noqa: E402
from guest_cleanup import cleanup_group, group_members

WORKFLOWS = ('lifecycle', 'image', 'volume', 'build', 'net')
EXPERIMENT_LABEL = 'harddind-experiment'
OUTPUT_CAP = 16384

NATIVE_REASON = ('no usable native Docker endpoint or host docker socket exists inside this '
                 'unprivileged container; host process launch timing is not Docker latency')
KERNEL_COMPARISON_NOTE = ('single kernel per run; rerun the same shard with another fixed '
                          '--kernel and combine reports with scripts/analyze-workflow-timing.py')

# Every observation must end with every named check true and every required
# stage recorded. The pure verifier tests feed crafted evidence through
# finalize_observation() so missing or failed output cannot become a pass.
REQUIRED_CHECKS = {
    'lifecycle': ('create_identity', 'logs_marker', 'exec_output', 'exec_hostname_identity',
                  'exec_digest', 'wait_exit_code', 'restart_running', 'restart_persisted_output',
                  'restart_changed_started_at', 'identity_stable', 'removed'),
    'image': ('import_id', 'base_runs', 'committed_payload_digest', 'save_artifact',
              'load_id_equal', 'loaded_runs', 'removed'),
    'volume': ('exec_digest', 'second_container_digest', 'host_roundtrip_digest', 'removed'),
    'build': ('cold_id', 'warm_content_equal', 'cold_run_digest', 'warm_run_digest',
              'warm_cache_marker', 'removed'),
    'net': ('server_running', 'dns_resolved', 'http_body_token', 'removed'),
}
REQUIRED_STAGES = {
    'lifecycle': ('create', 'start', 'logs_try0', 'exec_marker', 'wait', 'stop',
                  'restart_stopped', 'exec_persist', 'restart', 'exec_after_restart',
                  'final_stop', 'rm'),
    'image': ('import', 'base_run', 'create_data', 'cp_payload', 'commit', 'save',
              'rmi_committed_pre_load', 'load', 'loaded_run', 'rm_data_container',
              'rmi_committed_post', 'rmi_imported'),
    'volume': ('volume_create', 'create', 'cp_in', 'start', 'exec_digest', 'second_container',
               'cp_out', 'stop', 'rm', 'volume_rm'),
    'build': ('build_cold', 'build_warm', 'content_cold', 'content_warm', 'run_cold', 'run_warm', 'rmi_cold', 'rmi_warm'),
    'net': ('network_create', 'server_start', 'server_running', 'server_logs', 'dns_try0', 'http_try0', 'server_stop',
            'server_rm', 'network_rm'),
}

CURRENT_PROCS = set()


class Deadline(Exception):
    """Monotonic workload budget expired."""


class Aborted(Exception):
    """Outer supervisor asked the runner to terminate."""


class CommandError(Exception):
    def __init__(self, component):
        super().__init__(component['stage'] + ' failed')
        self.component = component


# --------------------------------------------------------------------------
# Pure helpers (unit tested with no guest and no docker binary)
# --------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fixed_payload(seed, size):
    """Deterministic non-compressible payload; every end hashes the same bytes."""
    if size <= 0 or size % 32:
        raise ValueError('payload size must be a positive multiple of 32')
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(seed.encode() + counter.to_bytes(8, 'big')).digest())
        counter += 1
    return bytes(out[:size])


def digest_line(text, path):
    """Parse busybox/coreutils ``sha256sum`` output for one named path."""
    match = re.search(r'(?m)^([0-9a-f]{64})[ \t]+(\*| )?' + re.escape(path) + r'\s*$', text)
    return match.group(1) if match else None


def alpine_release_ok(text):
    return text.strip() == '3.24.2'


def parse_nslookup(text, name):
    """A real embedded-DNS answer names the peer and gives a non-loopback IPv4."""
    if not re.search(r'127\.0\.0\.11', text):
        return None
    addresses = sorted({a for a in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
                        if not a.startswith('127.')})
    if name in text and addresses:
        return addresses
    return None


def verify_rootfs(path, expected_sha):
    if not path.is_file():
        raise ValueError('rootfs fixture is missing: ' + str(path))
    digest = sha256_file(path)
    if digest != expected_sha:
        raise ValueError('rootfs sha256 mismatch for %s: got %s expected %s'
                         % (path, digest, expected_sha))
    return {'path': str(path), 'sha256': digest, 'bytes': path.stat().st_size}


def parse_shards(text):
    items = text.split(',')
    names = [item.strip() for item in items]
    if (not names or any(not name for name in names)
            or any(name not in WORKFLOWS for name in names)
            or len(names) != len(set(names))):
        raise ValueError('shards must be a comma list of: ' + ','.join(WORKFLOWS))
    return tuple(names)


def effective_timeout(deadline, now, per_command):
    return max(0.0, min(float(per_command), deadline - now))


def capped_output(text, cap=OUTPUT_CAP):
    raw = text.encode(errors='replace')
    if len(raw) <= cap:
        return text, False, len(raw)
    return raw[:cap].decode(errors='replace') + '\n…[truncated]', True, len(raw)


def component_ok(comp, need_output=False):
    """Usable only when it ran, returned an accepted status and produced output
    when output was required. Timeout or empty required output is a failure."""
    if comp.get('status') != 'ok' or comp.get('timed_out') or not comp.get('cleanup', {}).get('clean_shutdown', True):
        return False
    if comp.get('exit_code') not in comp.get('expect', (0,)):
        return False
    if need_output and not comp.get('stdout', '').strip():
        return False
    return True


def build_image_content(text):
    """Compare executed image data; a new attestation may change the index ID."""
    info = json.loads(text)
    layers = info.get('RootFS', {}).get('Layers')
    if (not isinstance(layers, list) or not layers
            or not all(isinstance(v, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', v) for v in layers)
            or not isinstance(info.get('Config'), dict)
            or not info.get('Os') or not info.get('Architecture')):
        raise ValueError('missing image layers or execution configuration')
    return {key: info.get(key) for key in ('RootFS', 'Config', 'Os', 'Architecture', 'Variant')}


def object_missing(component):
    """Only the actual CLI no-such-object error proves removal, not any rc=1."""
    if not component_ok(component) or component.get('exit_code') != 1:
        return False
    message = (component.get('stdout', '') + component.get('stderr', '')).lower()
    return ('no such ' in message
            or re.search(r'(?m)^error response from daemon: network [a-z0-9_.-]+ not found$', message) is not None)



def finalize_observation(obs, workflow):
    """Derive pass/fail/timeout/not_run from the recorded raw evidence."""
    if obs.get('status') == 'not_run_wall_budget':
        obs['reaped_cleanly'] = not obs.get('cleanup_errors')
        obs.setdefault('missing_checks', list(REQUIRED_CHECKS[workflow]))
        obs.setdefault('missing_stages', list(REQUIRED_STAGES[workflow]))
        return obs
    stages = {c['stage'] for c in obs['components'] if not c.get('synthetic')}
    checks = {c['name']: c['ok'] for c in obs['checks']}
    hard_failures = [c for c in obs['components']
                     if not c.get('tolerated') and not component_ok(c)]
    missing_checks = [name for name in REQUIRED_CHECKS[workflow] if name not in checks]
    missing_stages = [s for s in REQUIRED_STAGES[workflow]
                      if s not in stages and not any(x.startswith(s + '_try') for x in stages)]
    if obs.get('error') or hard_failures or obs.get('cleanup_errors') or any(c.get('timed_out') for c in obs['components']):
        if any(c.get('timed_out') for c in obs['components']):
            obs['status'] = 'timeout'
        else:
            obs['status'] = 'fail'
    elif missing_checks or missing_stages:
        obs['status'] = 'incomplete_fail'
    elif not all(checks.values()):
        obs['status'] = 'fail'
    else:
        obs['status'] = 'pass'
    obs['missing_checks'] = missing_checks
    obs['missing_stages'] = missing_stages
    obs['reaped_cleanly'] = not obs.get('cleanup_errors')
    return obs


def list_problems(workflows):
    """Failed, timed-out, incomplete and never-run observations are all listed."""
    problems = []
    for name, data in workflows.items():
        for obs in data['observations']:
            status = obs.get('status', 'unknown')
            if status != 'pass':
                problems.append({'workflow': name, 'rep': obs['rep'], 'kind': status,
                                 'detail': obs.get('error') or 'observation did not pass'})
            # A never-run observation is already listed wholesale; its missing
            # checks/stages are not separate failures.
            if status != 'not_run_wall_budget':
                for missing in obs.get('missing_checks', []):
                    problems.append({'workflow': name, 'rep': obs['rep'], 'kind': 'missing_check',
                                     'detail': missing})
                for missing in obs.get('missing_stages', []):
                    problems.append({'workflow': name, 'rep': obs['rep'], 'kind': 'missing_stage',
                                     'detail': missing})
            for comp in obs['components']:
                if comp.get('status') in ('fail', 'timeout'):
                    problems.append({'workflow': name, 'rep': obs['rep'], 'kind': 'component_failure',
                                     'detail': comp['stage'], 'tolerated_retry': comp.get('tolerated', False),
                                     'exit_code': comp.get('exit_code'), 'timed_out': comp.get('timed_out')})
            for check in obs.get('checks', []):
                if not check['ok']:
                    problems.append({'workflow': name, 'rep': obs['rep'], 'kind': 'check_failed',
                                     'detail': check['name'] + ': ' + check.get('detail', '')[:200]})
            for error in obs.get('cleanup_errors', []):
                problems.append({'workflow': name, 'rep': obs['rep'], 'kind': 'cleanup_timeout',
                                 'detail': error})
    return problems


def summarize(workflows):
    summary = {}
    for name, data in workflows.items():
        observations = data['observations']
        counts = {}
        for obs in observations:
            counts[obs['status']] = counts.get(obs['status'], 0) + 1
        stages = {}
        for obs in observations:
            for comp in obs['components']:
                if comp.get('synthetic'):
                    continue
                stages.setdefault(comp['stage'], []).append(comp['seconds'])
        stage_stats = {stage: {'n': len(values), 'min_seconds': min(values),
                               'median_seconds': statistics.median(values),
                               'max_seconds': max(values)}
                       for stage, values in sorted(stages.items())}
        body_seconds = [obs.get('body_seconds', 0.0) for obs in observations
                        if obs['status'] != 'not_run_wall_budget']
        summary[name] = {
            'counts': counts, 'passed': counts.get('pass', 0),
            'repeated_observations': counts.get('pass', 0) >= 3,
            'stage_seconds': stage_stats,
            'observation_body_seconds': ({'n': len(body_seconds), 'min_seconds': min(body_seconds),
                                          'median_seconds': statistics.median(body_seconds),
                                          'max_seconds': max(body_seconds)}
                                         if body_seconds else None),
        }
    return summary


def exact_cleanup_commands(objects):
    """Plan exact removals for objects this run registered.

    Never a prune, never a filter-based mass removal, never an id-less rm.
    Containers first (volumes/networks are in use), images by unique tag last.
    """
    ordered_kind = ('container', 'volume', 'network', 'image')
    seen = set()
    plan = []
    for kind in ordered_kind:
        for obj in objects:
            if obj['kind'] != kind:
                continue
            key = (kind, obj['name'])
            if key in seen:
                continue
            seen.add(key)
            if kind == 'container':
                plan.append(('container', ['rm', '-f', obj['name']]))
            elif kind == 'volume':
                plan.append(('volume', ['volume', 'rm', obj['name']]))
            elif kind == 'network':
                plan.append(('network', ['network', 'rm', obj['name']]))
            else:
                plan.append(('image', ['rmi', obj['name']]))
    return plan


def overall_passed(report):
    """The strict structural gate; near-native/full acceptance is never granted."""
    if report.get('near_native_acceptance') is not False:
        return False
    if report.get('full_acceptance') is not False:
        return False
    if report.get('native_docker_comparison') != 'not_tested':
        return False
    if report.get('performance_threshold_enforced') is not False:
        return False
    if report.get('reps_requested', 0) < 3:
        return False
    if (report.get('runner_error') or report.get('setup', {}).get('error')
            or report.get('aborted') or report.get('leftovers', {}).get('scan_failed')
            or report.get('inputs_unchanged') is not True):
        return False
    expected = set(report.get('shards', ()))
    if not expected or not 1 <= report.get('reps_requested', 0) <= 10:
        return False
    if set(report['workflows']) != expected:
        return False
    for data in report['workflows'].values():
        obs_list = data['observations']
        if len(obs_list) != report['reps_requested'] or sorted(o['rep'] for o in obs_list) != list(range(report['reps_requested'])):
            return False
        if any(obs['status'] != 'pass' or not obs.get('reaped_cleanly', False) for obs in obs_list):
            return False
    instance = report.get('instance_cleanup', {})
    if not instance.get('clean_shutdown') or instance.get('forced_shutdown'):
        return False
    if report.get('leftovers', {}).get('objects'):
        return False
    return True


# --------------------------------------------------------------------------
# Docker CLI driving
# --------------------------------------------------------------------------

class Client:
    def __init__(self, base, log, deadline, timeouts, rawdir):
        self.base = base
        self.log = log
        self.deadline = deadline
        self.timeouts = timeouts
        self.rawdir = rawdir
        self.sequence = 0

    def wrap(self, docker_args):
        return [*self.base, *docker_args]

    def remaining(self, per_command):
        return effective_timeout(self.deadline, time.monotonic(), per_command)

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate(timeout=10)
        return out, err, proc.returncode

    def _popen(self, cmd, data=None):
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        CURRENT_PROCS.add(proc)
        return proc

    def component(self, stage, docker_args, rc, out, err, started, timed_out,
                  expect=(0,), tolerated=False, overlaps=(), seconds=None):
        self.sequence += 1
        raw = {}
        for channel, payload in [('stdout', out), ('stderr', err)]:
            path = self.rawdir / ('%04d-%s.%s' % (self.sequence, stage, channel))
            with path.open('xb') as f: f.write(payload)
            raw[channel] = {'path': str(path), 'sha256': sha256_bytes(payload), 'bytes': len(payload)}
        stdout = out.decode(errors='replace')
        stderr = err.decode(errors='replace')
        cap_out, trunc_out, out_bytes = capped_output(stdout)
        cap_err, trunc_err, err_bytes = capped_output(stderr)
        status = 'ok' if not timed_out and rc in expect else ('timeout' if timed_out else 'fail')
        comp = {'stage': stage, 'docker_args': docker_args, 'exit_code': rc,
                'timed_out': timed_out, 'seconds': time.monotonic() - started if seconds is None else seconds,
                'stdout': cap_out, 'stderr': cap_err, 'stdout_total_bytes': out_bytes,
                'stderr_total_bytes': err_bytes, 'truncated': trunc_out or trunc_err,
                'expect': list(expect), 'tolerated': tolerated, 'status': status,
                'overlaps': list(overlaps), 'raw': raw}
        self.log.write('$ ' + shlex.join(self.wrap(docker_args)) + '\n')
        self.log.write(stdout)
        if stderr:
            self.log.write('--- stderr ---\n' + stderr)
        self.log.write('[stage=%s exit=%s timed_out=%s seconds=%.6f]\n'
                       % (stage, rc, timed_out, comp['seconds']))
        self.log.flush()
        return comp

    def call(self, obs, stage, docker_args, *, data=None, timeout=None, expect=(0,),
             need_output=False, tolerate=False, overlaps=(), ignore_budget=False):
        per_command = timeout or self.timeouts['command']
        remaining = (per_command if ignore_budget
                     else self.remaining(per_command))
        if remaining <= 0:
            comp = {'stage': stage, 'docker_args': docker_args, 'exit_code': None,
                    'timed_out': False, 'seconds': 0.0, 'stdout': '', 'stderr': '',
                    'stdout_total_bytes': 0, 'stderr_total_bytes': 0, 'truncated': False,
                    'expect': list(expect), 'tolerated': tolerate, 'status': 'not_run',
                    'synthetic': True, 'overlaps': list(overlaps)}
            obs['components'].append(comp)
            raise Deadline(stage)
        cmd = self.wrap(docker_args)
        started = time.monotonic()
        proc = None
        try:
            proc = self._popen(cmd, data)
            if data is not None:
                out, err = proc.communicate(input=data, timeout=remaining)
            else:
                out, err = proc.communicate(timeout=remaining)
            rc, timed_out = proc.returncode, False
        except subprocess.TimeoutExpired:
            timed_out = True
            out, err, rc = self._kill(proc)
        finally:
            elapsed = time.monotonic() - started
            audit = None
            if proc is not None:
                if proc.poll() is None:
                    self._kill(proc)
                audit = cleanup_group(proc.pid)
                CURRENT_PROCS.discard(proc)
        comp = self.component(stage, docker_args, rc, out, err, started, timed_out,
                              expect=expect, tolerated=tolerate, overlaps=overlaps, seconds=elapsed)
        comp['pid'] = proc.pid
        comp['cleanup'] = audit
        obs['components'].append(comp)
        if not tolerate and not component_ok(comp, need_output=need_output):
            raise CommandError(comp)
        return comp

    def spawn(self, docker_args):
        cmd = self.wrap(docker_args)
        self.log.write('$ ' + shlex.join(cmd) + ' &\n')
        self.log.flush()
        started = time.monotonic()
        proc = self._popen(cmd)
        proc._harddind_started = started
        return proc, started

    def reap_spawned(self, proc):
        try:
            out, err = proc.communicate(timeout=self.remaining(self.timeouts['command']))
            return proc.returncode, out, err, False
        except subprocess.TimeoutExpired:
            out, err, rc = self._kill(proc)
            return rc, out, err, True
        finally:
            proc._harddind_seconds = time.monotonic() - proc._harddind_started
            if proc.poll() is None:
                self._kill(proc)
            proc._harddind_cleanup = cleanup_group(proc.pid)
            CURRENT_PROCS.discard(proc)


class Ctx:
    def __init__(self, client, obs, rep, names, rawdir):
        self.client = client
        self.obs = obs
        self.rep = rep
        self.names = names
        self.rawdir = rawdir

    def run(self, stage, args, **kwargs):
        return self.client.call(self.obs, stage, args, **kwargs)

    def labels(self, workflow):
        return ['--label', self.names['label'], '--label', 'harddind-workflow=' + workflow]

    def check(self, name, ok, detail=''):
        self.obs['checks'].append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:300]})

    def register(self, kind, name):
        for obj in self.obs['objects']:
            if obj['kind'] == kind and obj['name'] == name:
                return
        self.obs['objects'].append({'kind': kind, 'name': name})


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------

def workflow_lifecycle(ctx, base_tag, rootfs_info, payloads):
    n = ctx.names
    c = '%s-lc%d' % (n['prefix'], ctx.rep)
    token = ('wftoken-%d-' % ctx.rep) + hashlib.sha256(
        (n['experiment'] + ':lifecycle:' + str(ctx.rep)).encode()).hexdigest()[:16]
    ctx.register('container', c)
    created = ctx.run('create', ['create', '--name', c, *ctx.labels('lifecycle'), base_tag,
                                 'sh', '-c', 'echo wf-started; sleep 300'],
                      need_output=True)['stdout'].strip()
    ctx.check('create_identity', bool(re.fullmatch(r'[0-9a-f]{64}', created or '')), created[:24])
    identity = ctx.run('identity_after_create', ['inspect', '-f', '{{.Id}}', c],
                       need_output=True)['stdout'].strip().removeprefix('sha256:')
    ctx.run('start', ['start', c], need_output=True)
    logs = ''
    for attempt in range(3):
        logs_comp = ctx.run('logs_try%d' % attempt, ['logs', c], tolerate=True)
        logs = logs_comp['stdout']
        if 'wf-started' in logs:
            break
        time.sleep(0.5)
    ctx.check('logs_marker', 'wf-started' in logs, logs.strip()[:80])
    marker = ctx.run(
        'exec_marker',
        ['exec', c, 'sh', '-c',
         'printf %s ' + shlex.quote(token) + ' >/tmp/t; cat /tmp/t; echo; cat /etc/hostname; '
         'echo; sha256sum /tmp/t'], need_output=True)['stdout']
    lines = marker.splitlines()
    ctx.check('exec_output', lines[:1] == [token], lines[:1])
    ctx.check('exec_hostname_identity', len(lines) > 1 and lines[1] == identity[:12],
              '%r != %r' % (lines[1:2], identity[:12]))
    ctx.check('exec_digest', digest_line(marker, '/tmp/t') == sha256_bytes(token.encode()),
              digest_line(marker, '/tmp/t'))
    # docker wait is started first and overlaps stop on purpose: it observes
    # the real post-stop exit code delivered through the API.
    waiter, wait_started = ctx.client.spawn(['wait', c])
    time.sleep(0.2)
    ctx.run('stop', ['stop', '-t', '1', c], need_output=True)
    rc, out, err, timed_out = ctx.client.reap_spawned(waiter)
    wait_comp = ctx.client.component('wait', ['wait', c], rc, out, err, wait_started, timed_out,
                                     overlaps=('stop',), seconds=waiter._harddind_seconds)
    wait_comp['pid'] = waiter.pid
    wait_comp['cleanup'] = waiter._harddind_cleanup
    ctx.obs['components'].append(wait_comp)
    if not component_ok(wait_comp, need_output=True):
        raise CommandError(wait_comp)
    wait_code = out.decode().strip()
    stopped = ctx.run('inspect_after_stop',
                      ['inspect', '-f', '{{.State.ExitCode}} {{.State.Running}}', c],
                      need_output=True)['stdout'].strip()
    ctx.check('wait_exit_code', wait_code in ('137', '143') and stopped == wait_code + ' false',
              '%r %r' % (wait_code, stopped))
    ctx.run('restart_stopped', ['start', c], need_output=True)
    running = ctx.run('inspect_running', ['inspect', '-f', '{{.State.Running}}', c],
                      need_output=True)['stdout'].strip()
    before = ctx.run('started_at_before', ['inspect', '-f', '{{.State.StartedAt}}', c],
                     need_output=True)['stdout'].strip()
    persisted = ctx.run('exec_persist', ['exec', c, 'cat', '/tmp/t'],
                        need_output=True)['stdout']
    ctx.check('restart_running', running == 'true', running)
    ctx.check('restart_persisted_output', persisted.strip() == token, persisted[:40])
    ctx.run('restart', ['restart', '-t', '1', c], need_output=True)
    after_out = ctx.run('exec_after_restart',
                        ['exec', c, 'sh', '-c', 'cat /tmp/t; echo; cat /etc/hostname'],
                        need_output=True)['stdout'].splitlines()
    after = ctx.run('inspect_after_restart',
                    ['inspect', '-f', '{{.State.Running}} {{.State.StartedAt}} {{.Id}}', c],
                    need_output=True)['stdout'].strip().split()
    ctx.check('restart_changed_started_at',
              len(after) >= 3 and after[0] == 'true' and after[1] != before
              and after[2].removeprefix('sha256:') == identity,
              '%r vs %r' % (after[:2], before))
    ctx.check('identity_stable',
              after_out[:1] == [token] and len(after_out) > 1 and after_out[1] == identity[:12],
              after_out[:2])
    ctx.run('final_stop', ['stop', '-t', '1', c], need_output=True)
    ctx.run('rm', ['rm', c], need_output=True)
    gone = ctx.run('inspect_gone', ['inspect', c], expect=(1,), tolerate=True)
    gone_text = gone['stderr'] + gone['stdout']
    ctx.check('removed', object_missing(gone), gone_text[:120])


def workflow_image(ctx, base_tag, rootfs_info, payloads):
    n = ctx.names
    rep = ctx.rep
    repo = '%s-img%d' % (n['prefix'], rep)
    data_tag = repo + '-data'
    container = '%s-id%d' % (n['prefix'], rep)
    save_path = ctx.rawdir / ('save-%d.tar' % rep)
    ctx.register('image', repo)
    ctx.register('container', container)
    ctx.register('image', data_tag)
    for suffix in ['-base', '-loaded']: ctx.register('container', container + suffix)
    with open(rootfs_info['path'], 'rb') as f:
        rootfs = f.read()
    imported = ctx.run('import', ['import', '--change', 'LABEL ' + n['label'],
                                  '--change', 'LABEL harddind-workflow=image', '-', repo],
                       data=rootfs, timeout=ctx.client.timeouts['import'],
                       need_output=True)['stdout'].strip()
    ctx.check('import_id', bool(re.fullmatch(r'sha256:[0-9a-f]{64}', imported or '')),
              imported[:24])
    release = ctx.run('base_run', ['run', '--rm', '--name', container + '-base', *ctx.labels('image'), repo, 'cat', '/etc/alpine-release'],
                      need_output=True)['stdout']
    ctx.check('base_runs', alpine_release_ok(release), release.strip())
    ctx.run('create_data', ['create', '--name', container, '--label', n['label'], repo, 'true'])
    ctx.run('cp_payload', ['cp', str(ctx.rawdir / 'payload-1m.bin'), container + ':/payload'])
    ctx.run('commit', ['commit', '--change', 'LABEL ' + n['label'],
                       '--change', 'LABEL harddind-workflow=image', container, data_tag],
            need_output=True)
    id_before = ctx.run('id_before_save', ['inspect', '-f', '{{.Id}}', data_tag],
                        need_output=True)['stdout'].strip().removeprefix('sha256:')
    ctx.run('save', ['save', '-o', str(save_path), data_tag],
            timeout=ctx.client.timeouts['save'])
    save_stat = save_path.stat() if save_path.exists() else None
    save_hash = sha256_file(save_path) if save_stat else None
    ctx.check('save_artifact', bool(save_stat and save_stat.st_size > 1024 and save_hash),
              '%s %s' % (save_stat.st_size if save_stat else 'missing', save_hash))
    ctx.obs['artifacts'] = {'save_tar': {'path': str(save_path), 'bytes': save_stat.st_size,
                                          'sha256': save_hash}}
    ctx.run('rmi_committed_pre_load', ['rmi', data_tag])
    gone = ctx.run('inspect_gone_pre_load', ['inspect', data_tag], expect=(1,), tolerate=True)
    if not object_missing(gone):
        raise CommandError(gone)
    ctx.run('load', ['load', '-i', str(save_path)],
            timeout=ctx.client.timeouts['save'], need_output=True)
    id_after = ctx.run('id_after_load', ['inspect', '-f', '{{.Id}}', data_tag],
                       need_output=True)['stdout'].strip().removeprefix('sha256:')
    ctx.check('load_id_equal', bool(re.fullmatch(r'[0-9a-f]{64}', id_after or ''))
              and id_after == id_before, '%r vs %r' % (id_after[:20], id_before[:20]))
    loaded = ctx.run('loaded_run',
                     ['run', '--rm', '--name', container + '-loaded', *ctx.labels('image'), data_tag, 'sh', '-c',
                      'cat /etc/alpine-release; sha256sum /payload'],
                     need_output=True)['stdout']
    ctx.check('loaded_runs', alpine_release_ok(loaded.splitlines()[0] if loaded else ''),
              loaded.splitlines()[:1])
    ctx.check('committed_payload_digest',
              digest_line(loaded, '/payload') == payloads['one_mib']['sha256'],
              digest_line(loaded, '/payload'))
    ctx.run('rm_data_container', ['rm', '-f', container])
    ctx.run('rmi_committed_post', ['rmi', data_tag])
    ctx.run('rmi_imported', ['rmi', repo])
    remaining = []
    for tag in (data_tag, repo):
        probe = ctx.run('inspect_gone_%s' % tag.replace(':', '_'), ['inspect', tag],
                        expect=(1,), tolerate=True)
        if object_missing(probe):
            continue
        remaining.append(tag)
    ctx.check('removed', not remaining, remaining)


def workflow_volume(ctx, base_tag, rootfs_info, payloads):
    n = ctx.names
    rep = ctx.rep
    volume = '%s-vol%d' % (n['prefix'], rep)
    writer = '%s-vc%d' % (n['prefix'], rep)
    reader = '%s-vd%d' % (n['prefix'], rep)
    back_path = ctx.rawdir / ('payload-back-%d.bin' % rep)
    ctx.register('volume', volume)
    ctx.register('container', writer)
    ctx.register('container', reader)
    ctx.run('volume_create', ['volume', 'create', '--label', n['label'],
                              '--label', 'harddind-workflow=volume', volume], need_output=True)
    ctx.run('create', ['create', '--name', writer, *ctx.labels('volume'),
                       '-v', volume + ':/data', base_tag, 'sleep', '300'])
    ctx.run('cp_in', ['cp', str(ctx.rawdir / 'payload-1m.bin'), writer + ':/data/payload.bin'])
    ctx.run('start', ['start', writer], need_output=True)
    first = ctx.run('exec_digest', ['exec', writer, 'sha256sum', '/data/payload.bin'],
                    need_output=True)['stdout']
    ctx.check('exec_digest', digest_line(first, '/data/payload.bin')
              == payloads['one_mib']['sha256'], digest_line(first, '/data/payload.bin'))
    second = ctx.run(
        'second_container',
        ['run', '--rm', '--name', reader, *ctx.labels('volume'),
         '-v', volume + ':/data', base_tag, 'sha256sum', '/data/payload.bin'],
        need_output=True)['stdout']
    ctx.check('second_container_digest', digest_line(second, '/data/payload.bin')
              == payloads['one_mib']['sha256'], digest_line(second, '/data/payload.bin'))
    ctx.run('cp_out', ['cp', writer + ':/data/payload.bin', str(back_path)])
    back_ok = (back_path.exists() and back_path.stat().st_size == payloads['one_mib']['bytes']
               and sha256_file(back_path) == payloads['one_mib']['sha256'])
    ctx.check('host_roundtrip_digest', back_ok,
              '%s %s' % (back_path.exists(), sha256_file(back_path) if back_path.exists() else ''))
    ctx.obs['artifacts'] = {'cp_back': {'path': str(back_path),
                                         'bytes': back_path.stat().st_size if back_path.exists() else 0,
                                         'sha256': sha256_file(back_path) if back_path.exists() else None}}
    ctx.run('stop', ['stop', '-t', '2', writer], need_output=True)
    ctx.run('rm', ['rm', '-f', writer])
    ctx.run('volume_rm', ['volume', 'rm', volume], need_output=True)
    c_gone = ctx.run('container_inspect_gone', ['inspect', writer], expect=(1,), tolerate=True)
    v_gone = ctx.run('volume_inspect_gone', ['volume', 'inspect', volume],
                     expect=(1,), tolerate=True)
    ctx.check('removed', object_missing(c_gone) and object_missing(v_gone),
              '%d %d' % (c_gone['exit_code'], v_gone['exit_code']))


def workflow_build(ctx, base_tag, rootfs_info, payloads):
    n = ctx.names
    rep = ctx.rep
    cold_tag = '%s-bc%d' % (n['prefix'], rep)
    warm_tag = '%s-bw%d' % (n['prefix'], rep)
    context = ctx.rawdir / ('ctx-%d' % rep)
    context.mkdir(exist_ok=False)
    with (context / 'payload.bin').open('xb') as f:
        f.write(payloads['64k']['data'])
    dockerfile = (
        'FROM @BASE@\n'
        'LABEL @LABEL@\n'
        'LABEL harddind-workflow="build"\n'
        'WORKDIR /wf\n'
        'COPY payload.bin /wf/payload.bin\n'
        'RUN sha256sum /wf/payload.bin > /wf/payload.sha\n'
        'CMD ["cat", "/wf/payload.sha"]\n'
    ).replace('@BASE@', base_tag).replace('@LABEL@', n['label'])
    (context / 'Dockerfile').write_text(dockerfile)
    ctx.register('image', cold_tag)
    ctx.register('image', warm_tag)
    ctx.register('container', cold_tag + '-run')
    ctx.register('container', warm_tag + '-run')
    cold = ctx.run('build_cold', ['build', '--no-cache', '--progress=plain', '-t', cold_tag,
                                  str(context)], timeout=ctx.client.timeouts['build'])
    warm = ctx.run('build_warm', ['build', '--progress=plain', '-t', warm_tag, str(context)],
                   timeout=ctx.client.timeouts['build'])
    cold_id = ctx.run('id_cold', ['image', 'inspect', '-f', '{{.Id}}', cold_tag],
                      need_output=True)['stdout'].strip().removeprefix('sha256:')
    warm_id = ctx.run('id_warm', ['image', 'inspect', '-f', '{{.Id}}', warm_tag],
                      need_output=True)['stdout'].strip().removeprefix('sha256:')
    ctx.check('cold_id', bool(re.fullmatch(r'[0-9a-f]{64}', cold_id or '')), cold_id[:20])
    cold_info = ctx.run('content_cold', ['image', 'inspect', '-f', '{{json .}}', cold_tag], need_output=True)['stdout']
    warm_info = ctx.run('content_warm', ['image', 'inspect', '-f', '{{json .}}', warm_tag], need_output=True)['stdout']
    cold_content = build_image_content(cold_info)
    warm_content = build_image_content(warm_info)
    ctx.obs['build_image_content'] = {'cold': cold_content, 'warm': warm_content,
                                       'cold_id': cold_id, 'warm_id': warm_id}
    ctx.check('warm_content_equal', bool(re.fullmatch(r'[0-9a-f]{64}', warm_id))
              and cold_content == warm_content, 'RootFS, Config, OS, architecture and variant comparison')
    cold_run = ctx.run('run_cold', ['run', '--rm', '--name', cold_tag + '-run', *ctx.labels('build'), cold_tag, 'sha256sum', '/wf/payload.bin'], need_output=True)['stdout']
    warm_run = ctx.run('run_warm', ['run', '--rm', '--name', warm_tag + '-run', *ctx.labels('build'), warm_tag, 'sha256sum', '/wf/payload.bin'], need_output=True)['stdout']
    expected = payloads['64k']['sha256']
    ctx.check('cold_run_digest', digest_line(cold_run, '/wf/payload.bin') == expected,
              digest_line(cold_run, '/wf/payload.bin'))
    ctx.check('warm_run_digest', digest_line(warm_run, '/wf/payload.bin') == expected,
              digest_line(warm_run, '/wf/payload.bin'))
    cache_text = warm['stdout'] + warm['stderr']
    ctx.check('warm_cache_marker', 'CACHED' in cache_text, 'no CACHED marker in warm build')
    ctx.run('rmi_cold', ['rmi', cold_tag])
    ctx.run('rmi_warm', ['rmi', warm_tag])
    remaining = []
    for tag in (cold_tag, warm_tag):
        probe = ctx.run('inspect_gone_%d_%s' % (rep, tag[-3:]), ['image', 'inspect', tag],
                        expect=(1,), tolerate=True)
        if not object_missing(probe):
            remaining.append(tag)
    ctx.check('removed', not remaining, remaining)


def workflow_net(ctx, base_tag, rootfs_info, payloads):
    n = ctx.names
    rep = ctx.rep
    netname = '%s-net%d' % (n['prefix'], rep)
    srv = '%s-srv%d' % (n['prefix'], rep)
    token = hashlib.sha256((n['experiment'] + ':net:' + str(rep)).encode()).hexdigest()[:24]
    ctx.register('network', netname)
    ctx.register('container', srv)
    ctx.run('network_create', ['network', 'create', '--label', n['label'],
                               '--label', 'harddind-workflow=net', netname], need_output=True)
    server_script = ('mkdir -p /www && printf %s ' + shlex.quote(token + '\n')
                     + ' >/www/index.html && '
                     + 'bytes=$(wc -c < /www/index.html); '
                     + "{ printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\nConnection: close\\r\\n\\r\\n' \"$bytes\"; "
                     + 'cat /www/index.html; } >/www/response; '
                     + 'exec nc -lk -p 8080 -e cat /www/response')
    ctx.run('server_start', ['run', '-d', '--name', srv, '--network', netname,
                             *ctx.labels('net'), base_tag, 'sh', '-c', server_script],
            need_output=True)
    ctx.run('server_logs', ['logs', srv])
    running = ctx.run('server_running', ['inspect', '-f', '{{.State.Running}}', srv], need_output=True)['stdout'].strip()
    ctx.check('server_running', running == 'true', running)
    expected_ip = ctx.run('server_ip', ['inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', srv], need_output=True)['stdout'].strip()
    dns_addresses = None
    for attempt in range(3):
        client_name = srv + '-dns-' + str(attempt)
        ctx.register('container', client_name)
        probe = ctx.run('dns_try%d' % attempt, ['run', '--rm', '--name', client_name, *ctx.labels('net'), '--network', netname, base_tag,
                                                'nslookup', srv], tolerate=True)
        dns_addresses = parse_nslookup(probe['stdout'], srv) if component_ok(probe) else None
        if dns_addresses:
            break
        time.sleep(1.0)
    ctx.check('dns_resolved', bool(dns_addresses) and expected_ip in dns_addresses, {'expected': expected_ip, 'resolved': dns_addresses})
    http_body = None
    for attempt in range(6):
        client_name = srv + '-http-' + str(attempt)
        ctx.register('container', client_name)
        probe = ctx.run('http_try%d' % attempt,
                        ['run', '--rm', '--name', client_name, *ctx.labels('net'), '--network', netname, base_tag, 'sh', '-c',
                         'wget -q -T 5 -O - http://%s:8080/' % srv], tolerate=True)
        if probe['exit_code'] == 0 and probe['stdout'].strip() == token:
            http_body = probe['stdout'].strip()
            break
        time.sleep(1.0)
    ctx.check('http_body_token', http_body == token, repr(http_body))
    ctx.run('server_stop', ['stop', '-t', '2', srv])
    ctx.run('server_rm', ['rm', '-f', srv])
    ctx.run('network_rm', ['network', 'rm', netname])
    c_gone = ctx.run('container_inspect_gone', ['inspect', srv], expect=(1,), tolerate=True)
    n_gone = ctx.run('network_inspect_gone', ['network', 'inspect', netname],
                     expect=(1,), tolerate=True)
    ctx.check('removed', object_missing(c_gone) and object_missing(n_gone),
              '%d %d' % (c_gone['exit_code'], n_gone['exit_code']))


WORKFLOW_FUNCTIONS = {'lifecycle': workflow_lifecycle, 'image': workflow_image,
                      'volume': workflow_volume, 'build': workflow_build, 'net': workflow_net}


# --------------------------------------------------------------------------
# Observation execution and exact cleanup
# --------------------------------------------------------------------------

def cleanup_observation(client, obs):
    """Reap exactly this observation's registered objects; timeouts are kept."""
    for kind, args in exact_cleanup_commands(obs['objects']):
        comp = client.call(obs, 'cleanup_' + kind, args, expect=(0, 1), tolerate=True,
                           ignore_budget=True)
        if (not comp.get('cleanup', {}).get('clean_shutdown') or comp['timed_out']
                or (comp['exit_code'] != 0 and not object_missing(comp))):
            obs['cleanup_errors'].append('%s %s cleanup failed' % (kind, args[-1]))
        # A nonzero rc here is normally "already removed" by the body stages.


def run_observation(client, workflow, rep, names, rawdir, base_tag, rootfs_info, payloads):
    obs = {'rep': rep, 'components': [], 'checks': [], 'objects': [],
           'cleanup_components': [], 'cleanup_errors': [], 'error': None,
           'status': 'pending'}
    ctx = Ctx(client, obs, rep, names, rawdir)
    started = time.monotonic()
    try:
        WORKFLOW_FUNCTIONS[workflow](ctx, base_tag, rootfs_info, payloads)
    except Deadline:
        obs['error'] = 'workload_budget_exceeded'
    except CommandError as error:
        comp = error.component
        obs['error'] = ('component %s status=%s exit=%s timed_out=%s'
                        % (comp['stage'], comp['status'], comp['exit_code'], comp['timed_out']))
    except Aborted:
        obs['error'] = 'aborted'
        raise
    except Exception as error:  # noqa: BLE001 - evidence gathering must continue
        obs['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        obs['body_seconds'] = time.monotonic() - started
        cleanup_started = time.monotonic()
        for proc in list(CURRENT_PROCS):
            if proc.poll() is None: client._kill(proc)
            audit = cleanup_group(proc.pid)
            obs.setdefault('orphan_clients', []).append(audit)
            obs['cleanup_errors'].append('unfinished client process group %d' % proc.pid)
            CURRENT_PROCS.discard(proc)
        try:
            cleanup_observation(client, obs)
        except Deadline:
            obs['cleanup_errors'].append('cleanup skipped: workload budget expired')
        obs['cleanup_seconds'] = time.monotonic() - cleanup_started
    finalize_observation(obs, workflow)
    return obs


def not_run_observation(workflow, rep, reason):
    obs = {'rep': rep, 'components': [], 'checks': [], 'objects': [], 'cleanup_components': [],
           'cleanup_errors': [], 'error': reason, 'status': 'not_run_wall_budget',
           'body_seconds': 0.0, 'cleanup_seconds': 0.0, 'reaped_cleanly': True,
           'missing_checks': list(REQUIRED_CHECKS[workflow]),
           'missing_stages': list(REQUIRED_STAGES[workflow])}
    return obs


def host_command(log, cmd, timeout):
    """Run a harddind.py host-side (start/stop) command under a hard timeout."""
    log.write('$ ' + shlex.join(cmd) + '\n')
    log.flush()
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = False
    CURRENT_PROCS.add(proc)
    try:
        out = proc.communicate(timeout=timeout)[0]
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        out, _, rc = Client._kill(proc)
    finally:
        elapsed = time.monotonic() - started
        if proc.poll() is None: Client._kill(proc)
        audit = cleanup_group(proc.pid)
        CURRENT_PROCS.discard(proc)

    text = out.decode(errors='replace')
    log.write(text)
    log.write('[host exit=%s timed_out=%s seconds=%.6f]\n'
              % (rc, timed_out, elapsed))
    log.flush()
    return {'argv': cmd, 'exit_code': rc, 'timed_out': timed_out,
            'seconds': elapsed, 'output': text[-OUTPUT_CAP:], 'pid': proc.pid, 'cleanup': audit}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_report(a, repo, kernel, disk, rootfs_info, payloads):
    sources = [pathlib.Path(__file__).resolve(), repo / 'scripts/harddind.py',
               repo / 'scripts/guest_cleanup.py', repo / 'scripts/port_driver.py', repo / 'sources.lock.json']
    return {
        'kind': 'docker_workflow_timing',
        'experiment': a.name,
        'shards': list(a.shards),
        'reps_requested': a.reps,
        'full_acceptance': False,
        'near_native_acceptance': False,
        'native_docker_comparison': 'not_tested',
        'native_docker_comparison_reason': NATIVE_REASON,
        'kernel_comparison': {'status': 'single_kernel_only', 'note': KERNEL_COMPARISON_NOTE},
        'performance_threshold_enforced': False,
        'performance_note': 'raw timings only; this runner deliberately defines no pass threshold',
        'config': {'cpus': a.cpus, 'memory': a.memory, 'dns': a.dns, 'mtu': a.mtu,
                   'command_timeout_seconds': a.command_timeout,
                   'build_timeout_seconds': a.build_timeout,
                   'import_timeout_seconds': a.import_timeout,
                   'save_timeout_seconds': a.save_timeout,
                   'workload_budget_seconds': a.workload_budget},
        'host': {'cpu_affinity': sorted(os.sched_getaffinity(0)),
                 'python': '.'.join(map(str, sys.version_info[:3]))},
        'inputs': {
            'kernel': {'path': str(kernel), 'sha256': harddind.sha(kernel)},
            'disk': {'path': str(disk), 'sha256': harddind.sha(disk),
                     'mode': 'uml_cow_backing_file_not_opened_for_writing_by_runner'},
            'rootfs': rootfs_info,
            'payload_one_mib': {'bytes': payloads['one_mib']['bytes'],
                                'sha256': payloads['one_mib']['sha256'],
                                'seed': payloads['one_mib']['seed']},
            'payload_64kib': {'bytes': payloads['64k']['bytes'],
                              'sha256': payloads['64k']['sha256'],
                              'seed': payloads['64k']['seed']},
            'sources': {str(p.relative_to(repo)): harddind.sha(p) for p in sources}},
        'setup': {}, 'fixture_components': [], 'workflows': {}, 'leftovers': {},
        'instance_cleanup': {}, 'summary': {}, 'problems': [], 'subset_passed': False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True, help='immutable experiment/instance name (a-z0-9_-, max 32)')
    p.add_argument('--shards', required=True, help='comma list, e.g. lifecycle or image,build')
    p.add_argument('--reps', type=int, default=3)
    p.add_argument('--rootfs', type=pathlib.Path,
                   help='local pinned rootfs tar stream (default WORK/cache alpine fixture)')
    p.add_argument('--rootfs-sha256', help='required sha256 (default: sources.lock.json alpine)')
    p.add_argument('--cpus', type=int, default=4)
    p.add_argument('--memory', default='4G')
    p.add_argument('--dns', default='10.0.2.3')
    p.add_argument('--mtu', type=int, default=1500)
    p.add_argument('--command-timeout', type=float, default=40.0)
    p.add_argument('--build-timeout', type=float, default=90.0)
    p.add_argument('--import-timeout', type=float, default=90.0)
    p.add_argument('--save-timeout', type=float, default=90.0)
    p.add_argument('--start-timeout', type=float, default=170.0)
    p.add_argument('--stop-timeout', type=float, default=70.0)
    p.add_argument('--workload-budget', type=float, default=105.0,
                   help='seconds after instance readiness reserved for all workflows')
    a = p.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', a.name):
        p.error('name must match [a-z0-9][a-z0-9_-]{0,31}')
    import math
    for key, value in vars(a).items():
        if key.endswith('_timeout') or key == 'workload_budget':
            if not math.isfinite(value) or value <= 0:
                p.error(key + ' must be finite and positive')
    if not 1 <= a.reps <= 10:
        p.error('reps must be 1..10 (>=3 required for a repeatable timing claim)')
    try:
        a.shards = parse_shards(a.shards)
    except ValueError as error:
        p.error(str(error))
    if not 1 <= a.cpus <= 64:
        p.error('cpus must be 1..64')

    work = a.work.resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    kernel = a.kernel.resolve()
    disk = a.disk.resolve()
    lock_data = json.loads((repo / 'sources.lock.json').read_text())
    rootfs = (a.rootfs or (work / 'cache'
                           / 'alpine-minirootfs-3.24.2-x86_64.tar.gz')).resolve()
    rootfs_sha = a.rootfs_sha256 or lock_data['alpine']['sha256']
    try:
        rootfs_info = verify_rootfs(rootfs, rootfs_sha)
    except ValueError as error:
        p.error(str(error) + '; stage the pinned fixture via scripts/fetch-artifact.py or '
                'pass --rootfs/--rootfs-sha256 explicitly (the runner never downloads)')
    if not kernel.is_file():
        p.error('kernel is missing: ' + str(kernel))
    if not disk.is_file():
        p.error('disk is missing: ' + str(disk))

    # Builders take an exclusive lock; this runner only ever holds a shared
    # one for the whole process. It never takes an exclusive lock that could
    # block a child harddind serve process taking its own shared lock.
    build_lock = (work / 'kernel-build.lock').open('a')
    lock_deadline = time.monotonic() + 90
    while True:
        try:
            fcntl.flock(build_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= lock_deadline: raise RuntimeError('build lock wait exceeded 90 seconds')
            time.sleep(.1)

    results = work / 'results'
    results.mkdir(exist_ok=True)
    report_path = results / (a.name + '.json')
    log_path = results / (a.name + '.log')
    rawdir = results / (a.name + '-raw')
    instance = work / 'instances' / a.name
    if instance.exists() or report_path.exists() or log_path.exists() or rawdir.exists():
        p.error('refusing existing instance or evidence; choose a new immutable --name')
    rawdir.mkdir(parents=True)

    one_mib = fixed_payload('harddind-wf payload one-mib v1', 1024 * 1024)
    sixty_four_k = fixed_payload('harddind-wf payload build-ctx v1', 64 * 1024)
    with (rawdir / 'payload-1m.bin').open('xb') as f:
        f.write(one_mib)
    payloads = {
        'one_mib': {'data': one_mib, 'bytes': len(one_mib), 'seed': 'harddind-wf payload one-mib v1',
                    'sha256': sha256_bytes(one_mib), 'path': str(rawdir / 'payload-1m.bin')},
        '64k': {'data': sixty_four_k, 'bytes': len(sixty_four_k),
                'seed': 'harddind-wf payload build-ctx v1', 'sha256': sha256_bytes(sixty_four_k)}}
    names = {'experiment': a.name, 'prefix': 'harddind-wf-' + a.name,
             'label': EXPERIMENT_LABEL + '=' + a.name}

    report = build_report(a, repo, kernel, disk, rootfs_info, payloads)
    report['raw_dir'] = str(rawdir)
    input_paths = [kernel, disk, pathlib.Path(rootfs_info['path']),
                   work / 'network-backend', work / 'client/docker',
                   work / 'client/ld-musl-x86_64.so.1',
                   *(repo / n for n in report['inputs']['sources'])]
    plugin = work / 'client/cli-plugins/docker-buildx'
    if 'build' in a.shards: input_paths.append(plugin)
    report['input_hashes_before'] = {str(f): harddind.sha(f) for f in input_paths}
    log = log_path.open('x')
    base_cmd = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), a.name]
    start_cmd = base_cmd + ['start', '--kernel', str(kernel), '--disk', str(disk),
                            '--cpus', str(a.cpus), '--memory', a.memory,
                            '--dns', a.dns, '--mtu', str(a.mtu)]
    timeouts = {'command': a.command_timeout, 'build': a.build_timeout,
                'import': a.import_timeout, 'save': a.save_timeout}

    booted = False
    aborted = False
    client = None

    def handle_signal(signum, frame):
        for proc in list(CURRENT_PROCS):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise Aborted('signal %d' % signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    wall_started = time.monotonic()
    try:
        boot = host_command(log, start_cmd, a.start_timeout)
        report['setup']['boot'] = boot
        booted = boot['exit_code'] == 0 and not boot['timed_out']
        if not booted:
            report['setup']['error'] = 'instance did not reach ready state'
        else:
            deadline = time.monotonic() + a.workload_budget
            client = Client(base_cmd + ['docker'], log, deadline, timeouts, rawdir)
            fixture_obs = {'components': []}
            server = client.call(fixture_obs, 'server_version',
                                 ['version', '--format', '{{json .Server}}'],
                                 need_output=True, ignore_budget=True)
            report['server_version'] = json.loads(server['stdout'])
            base_tag = names['prefix'] + ':base'
            with open(rootfs_info['path'], 'rb') as f:
                rootfs_bytes = f.read()
            try:
                imported = client.call(
                    fixture_obs, 'fixture_import',
                    ['import', '--change', 'LABEL ' + names['label'],
                     '--change', 'LABEL harddind-workflow=base', '-', base_tag],
                    data=rootfs_bytes, timeout=a.import_timeout, need_output=True,
                    ignore_budget=True)
                report['setup']['base_image'] = {'tag': base_tag,
                                                  'image_id': imported['stdout'].strip()}
            except CommandError as error:
                report['setup']['error'] = 'fixture import failed: ' + error.component['stage']
            report['fixture_components'] = fixture_obs['components']

            if 'error' not in report['setup']:
                global_objects = [{'kind': 'image', 'name': base_tag}]
                budget_open = True
                for workflow in a.shards:
                    data = {'observations': []}
                    report['workflows'][workflow] = data
                    for rep in range(a.reps):
                        if not budget_open:
                            obs = not_run_observation(
                                workflow, rep, 'not scheduled: workload budget already consumed')
                        else:
                            try:
                                obs = run_observation(client, workflow, rep, names, rawdir,
                                                      base_tag, rootfs_info, payloads)
                            except Aborted:
                                raise
                            if obs['error'] == 'workload_budget_exceeded':
                                budget_open = False
                        finalize_observation(obs, workflow)
                        data['observations'].append(obs)

                # Remove the shared fixture image by exact tag, then scan for
                # anything still carrying this experiment's label. No prune.
                for kind, args in exact_cleanup_commands(global_objects):
                    client.call({'components': report['fixture_components']},
                                'cleanup_' + kind, args, expect=(0, 1), tolerate=True,
                                ignore_budget=True)
                leftovers = {}
                leftover_queries = (
                    ('containers', ['ps', '-aq', '--no-trunc', '--filter', 'label=' + names['label']]),
                    ('volumes', ['volume', 'ls', '-q', '--filter', 'label=' + names['label']]),
                    ('networks', ['network', 'ls', '-q', '--filter', 'label=' + names['label']]),
                    ('images', ['image', 'ls', '-q', '--filter', 'label=' + names['label']]))
                scan_obs = {'components': []}
                scan_failed = False
                for kind, args in leftover_queries:
                    comp = client.call(scan_obs, 'leftover_' + kind, args, tolerate=True,
                                       ignore_budget=True)
                    if comp['timed_out'] or comp['exit_code'] != 0:
                        scan_failed = True
                    leftovers[kind] = [line for line in comp['stdout'].splitlines() if line.strip()]
                report['leftover_components'] = scan_obs['components']
                report['leftovers'] = {
                    'objects': {kind: ids for kind, ids in leftovers.items() if ids},
                    'label': names['label'], 'scan_failed': scan_failed}
    except Aborted as error:
        aborted = True
        report['aborted'] = str(error)
    except Exception as error:  # noqa: BLE001 - report must still be written
        report['runner_error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        # Once teardown starts, a second supervisor signal must not prevent
        # the exact instance stop or the report from being written.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if booted or (instance / 'state.json').exists():
            stop = host_command(log, base_cmd + ['stop'], a.stop_timeout)
            state = harddind.read_state(instance)
            report['instance_cleanup'] = {
                'stop_command': stop, 'phase': state.get('phase'),
                'guest_exit_code': state.get('guest_exit_code'),
                'forced_shutdown': state.get('forced_shutdown'),
                'clean_shutdown': (
                    state.get('phase') == 'stopped'
                    and state.get('guest_cleanup', {}).get('clean_shutdown')
                    and state.get('network_cleanup', {}).get('clean_shutdown')
                    and state.get('guest_exit_code') == 0 and state.get('network_exit_code') == 0
                    and stop['exit_code'] == 0 and not stop['timed_out']
                    and stop['cleanup']['clean_shutdown']
                    and not state.get('forced_shutdown')),
                'remaining_live_pids': state.get('guest_cleanup', {}).get('remaining_live_pids')}
        else:
            report['instance_cleanup'] = {'clean_shutdown': False, 'forced_shutdown': None,
                                          'note': 'instance never reached ready state'}
        report['input_hashes_after'] = {str(f): harddind.sha(f) if f.is_file() else None for f in input_paths}
        report['inputs_unchanged'] = report['input_hashes_before'] == report['input_hashes_after']
        report['total_wall_seconds'] = time.monotonic() - wall_started
        report['summary'] = summarize(report['workflows'])
        report['problems'] = list_problems(report['workflows'])
        if report.get('leftovers', {}).get('scan_failed'):
            report['problems'].append({'workflow': '-', 'rep': -1, 'kind': 'leftover_scan_failed',
                                       'detail': 'leftover enumeration command failed/timed out'})
        if report.get('leftovers', {}).get('objects'):
            report['problems'].append({'workflow': '-', 'rep': -1, 'kind': 'leftover_objects',
                                       'detail': json.dumps(report['leftovers']['objects'])})
        if aborted:
            report['problems'].append({'workflow': '-', 'rep': -1, 'kind': 'aborted',
                                       'detail': report.get('aborted', 'signal')})
        if report.get('setup', {}).get('error') or report.get('runner_error'):
            report['problems'].append({'workflow': '-', 'rep': -1, 'kind': 'setup_fail',
                                       'detail': report.get('setup', {}).get('error')
                                       or report.get('runner_error')})
        report['subset_passed'] = (
            overall_passed(report) and not aborted and not report.get('runner_error')
            and not report.get('setup', {}).get('error')
            and not report.get('leftovers', {}).get('scan_failed'))
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        log.close()
    print(json.dumps({'report': str(report_path), 'log': str(log_path), 'raw_dir': str(rawdir),
                      'shards': report['summary'],
                      'problems': len(report['problems']),
                      'subset_passed': report['subset_passed'],
                      'full_acceptance': False, 'native_docker_comparison': 'not_tested'},
                     indent=2))
    return 0 if report['subset_passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error))
