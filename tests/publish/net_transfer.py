"""Optional pipeline diagnostics after scored samples, with persistent clients."""
import contextlib
import importlib.util
import json
import math
import os
import pathlib
import selectors
import subprocess
import threading
import time

from pipeline import build, config, runs

spec = importlib.util.spec_from_file_location('net_profile', pathlib.Path(__file__).with_name('net_profile.py'))
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def counters(text):
    lines = text.splitlines()
    if not lines:
        raise ValueError('empty network counters')
    names = lines[0].split()
    if names[:2] != ['cpu', 'tid'] or len(set(names)) != len(names):
        raise ValueError('invalid network counter header')
    result = {}
    for line in lines[1:]:
        values = [int(x) for x in line.split()]
        if len(values) != len(names) or min(values) < 0 or values[0] in result:
            raise ValueError('invalid network counter row')
        result[values[0]] = dict(zip(names, values))
    if not result:
        raise ValueError('no vCPU counters')
    return result


def counter_delta(before, after):
    if before.keys() != after.keys():
        raise ValueError('vCPU counter set changed')
    rows = {}
    for cpu, old in before.items():
        new = after[cpu]
        if old.keys() != new.keys() or old['tid'] != new['tid']:
            raise ValueError('vCPU counter identity changed')
        row = {key: new[key] - old[key] for key in old if key not in ('cpu', 'tid')}
        if min(row.values()) < 0:
            raise ValueError('network counter went backwards')
        rows[cpu] = row
    return {'per_cpu': rows, 'total': {key: sum(row[key] for row in rows.values())
                                      for key in next(iter(rows.values()))}}


class TaskSnapshot(dict):
    def __init__(self):
        super().__init__()
        self.errors = []


def tasks(group, root=pathlib.Path('/proc'), pids=None, *, follow_children=False, cpu_only=False):
    """Read task stats, never both process aggregates and their thread stats."""
    if pids is None:
        pids = [p['pid'] for p in profile.processes(group, root).values()]
    result = TaskSnapshot()
    pending, seen = list(pids), set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        paths = list((root / str(pid) / 'task').glob('[0-9]*/stat'))
        if not paths:
            result.errors.append({'pid': pid, 'error': 'no readable tasks'})
        for path in paths:
            try:
                text = path.read_text()
                item = profile.parse_stat(text)
                if item['pgrp'] != group:
                    continue
                if follow_children:
                    pending.extend(int(x) for x in (path.parent / 'children').read_text().split())
                with_status = not cpu_only or pid == group
                status = (dict(line.split(':', 1) for line in (path.parent / 'status').read_text().splitlines()
                               if ':' in line) if with_status else {})
                item.update(tgid=pid, tid=item['pid'], state=text.rsplit(') ', 1)[1].split()[0],
                            wchan=(path.parent / 'wchan').read_text().strip() if not cpu_only else '',
                            voluntary=int(status.get('voluntary_ctxt_switches', 0)) if with_status else None,
                            involuntary=int(status.get('nonvoluntary_ctxt_switches', 0)) if with_status else None)
                result[(item['tid'], item['start'])] = item
            except (OSError, ValueError, KeyError, IndexError) as error:
                result.errors.append({'path': str(path), 'error': str(error)})
    return result


def thread_delta(before, after, ticks, vcpus, samples=(), start_tick=None):
    latest = dict(before)
    for sample in samples:
        latest.update({(p['tid'], p['start']): p for p in sample['threads']})
    latest.update(after)
    initial = dict(before)
    for key, row in latest.items():
        if key not in initial and start_tick is not None and key[1] > start_tick:
            initial[key] = {**row, **dict.fromkeys(('user', 'system', 'voluntary', 'involuntary'), 0)}
    rows, groups = [], {}
    for key in sorted(initial.keys() & latest.keys()):
        a, b = initial[key], latest[key]
        kind = ('vcpu' if b['tid'] in vcpus else 'sigio_helper' if b['comm'] == 'uml-sigio'
                else 'userspace_runner' if b['comm'] == 'uml-userspace' else 'other_helper')
        row = {name: b[name] for name in ('tgid', 'tid', 'comm', 'state', 'wchan')}
        row.update(kind=kind, cpu=vcpus.get(b['tid']), exited=key not in after,
                   born_in_window=key not in before)
        for name in ('user', 'system', 'voluntary', 'involuntary'):
            if a[name] is None or b[name] is None:
                row[name] = None
                continue
            value = b[name] - a[name]
            if value < 0:
                raise ValueError('task counter went backwards')
            row[name] = value / ticks if name in ('user', 'system') else value
        totals = groups.setdefault(kind, dict.fromkeys(('user', 'system', 'voluntary', 'involuntary'), 0))
        for name in totals:
            totals[name] = totals[name] + row[name] if totals[name] is not None and row[name] is not None else None
        rows.append(row)
    errors = getattr(before, 'errors', []) + getattr(after, 'errors', [])
    return {'threads': rows, 'groups': groups, 'read_errors': errors,
            'new': [latest[k] for k in sorted(latest.keys() - before.keys())],
            'exited': [latest[k] for k in sorted(latest.keys() - after.keys())],
            'unaccounted': [latest[k] for k in sorted(latest.keys() - initial.keys())],
            'complete': initial.keys() == latest.keys() == after.keys() and bool(before) and not errors,
            'vcpu_complete': all(any(r['tid'] == tid and not r['exited'] and not r['born_in_window']
                                     for r in rows) for tid in vcpus) and bool(vcpus),
            'scope': 'task CPU; new tasks born after start tick included, exited tasks are lower bounds'}


@contextlib.contextmanager
def state_samples(group, pids):
    rows, stopped = [], threading.Event()
    def collect():
        while not stopped.wait(0.05):
            started = time.monotonic()
            snapshot = tasks(group, pids=pids, follow_children=True)
            rows.append({'at': started, 'threads': list(snapshot.values()), 'read_errors': snapshot.errors,
                         'read_seconds': time.monotonic() - started})
    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        yield rows, stopped
    finally:
        stopped.set(); thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError('task sampler did not stop')


def read_line(proc, timeout=40):
    # Unbuffered pipes: select must not overlook bytes in a Python read buffer.
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline, line = time.monotonic() + timeout, bytearray()
        while len(line) < 4096:
            if not selector.select(max(0, deadline - time.monotonic())):
                raise TimeoutError('transfer marker timed out')
            char = os.read(proc.stdout.fileno(), 1)
            if not char:
                raise RuntimeError('transfer client closed its output')
            if char == b'\n':
                return line.decode()
            line.extend(char)
    raise ValueError('transfer marker too long')


def result(line, count):
    item = json.loads(line)
    if isinstance(item.get('verified'), bool) or item.get('verified') != count or any(isinstance(item.get(k), bool) or
            not isinstance(item.get(k), (int, float)) or not math.isfinite(item[k]) or item[k] <= 0
            for k in ('seconds', 'value')):
        raise ValueError('invalid transfer result')
    return item


def binary():
    source = config.REPO / 'tests/publish/net_transfer.c'
    compiler = subprocess.run(['gcc', '--version'], check=True, capture_output=True, text=True).stdout
    key = build.key_of('net-transfer/1', source, config.REPO / 'bench/common.h', pathlib.Path(__file__), compiler)
    def compile_client(tmp, _final):
        build.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', source, '-o', tmp / 'net-transfer'],
                  log_file=tmp / 'build.log')
    return build.artifact('net-transfer', key, compile_client) / 'net-transfer'


def run(workloads, metric, address, port, options=()):
    """Call after network_samples' yield, while its server/network still exist."""
    ctx, mode = workloads.ctx, 'rr' if metric.endswith('_rr') else 'stream'
    count = 8192 if mode == 'rr' else 1024 ** 3
    state = json.loads((ctx.instance / 'state.json').read_text())
    guest_pid, backend_pid = state['guest_pid'], state['network_pid']
    client = binary()
    with contextlib.ExitStack() as stack:
        inspector = stack.enter_context(workloads.container(['--network', 'host']))
        workloads.docker('cp', str(client), inspector + ':/net-transfer')
        inspect = lambda: counters(workloads.docker('exec', inspector, 'cat', '/proc/uml_net_profile'))
        features = json.loads(workloads.docker('exec', inspector, '/net-transfer', 'inspect'))
        env = None
        if metric.startswith('net.publish'):
            argv = [str(client), address, str(port), mode]
        else:
            container = stack.enter_context(workloads.container(options))
            workloads.docker('cp', str(client), container + ':/net-transfer')
            argv, env = ctx.docker_cmd()
            argv += ['exec', '-i', container, '/net-transfer', address, str(port), mode]
        log = stack.enter_context((ctx.run_dir / (metric + '-transfer.stderr')).open('w'))
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=log, bufsize=0, preexec_fn=lambda: os.sched_setaffinity(0, workloads.cpus))
        try:
            for repeat in range(3):
                entry = {'metric': metric, 'repeat': repeat, 'count': count, 'complete': False,
                         'unix_time': time.time(), 'clock_ticks': os.sysconf('SC_CLK_TCK'),
                         'features': features, 'scope': 'READY/GO to verified DONE; excludes Docker creation/exec setup',
                         'counter_scope': 'guest exec snapshots bracket CPU window; includes snapshot overhead'}
                try:
                    proc.stdin.write(f'{count}\n'.encode())
                    if read_line(proc) != 'READY':
                        raise ValueError('missing READY marker')
                    pids = [p['pid'] for p in profile.processes(guest_pid).values()]
                    kernel_before = inspect()
                    backend_before = ctx.forward('stats')
                    start_tick = int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK'))
                    read_start = time.monotonic()
                    before = tasks(guest_pid, pids=pids, follow_children=True, cpu_only=True)
                    backend_cpu = tasks(backend_pid, pids=[backend_pid], cpu_only=True)
                    entry['before_snapshot_seconds'] = time.monotonic() - read_start
                    throttled = runs.throttle()
                    with state_samples(guest_pid, pids) as (samples, stopped):
                        started = time.monotonic()
                        proc.stdin.write(b'GO\n')
                        entry['workload'] = result(read_line(proc), count)
                        entry['marker_wall_seconds'] = time.monotonic() - started
                        stopped.set()
                        read_start = time.monotonic()
                        after = tasks(guest_pid, pids=pids, follow_children=True, cpu_only=True)
                        backend_after = tasks(backend_pid, pids=[backend_pid], cpu_only=True)
                        entry['snapshot_seconds'] = time.monotonic() - read_start
                        throttle_after = runs.throttle()
                    entry['throttled'] = ((throttle_after[1] - throttled[1]) / (throttle_after[0] - throttled[0])
                        if throttled and throttle_after and throttle_after[0] > throttled[0] else None)
                    ticks = os.sysconf('SC_CLK_TCK')
                    vcpus = {p['tid']: cpu for cpu, p in kernel_before.items()}
                    entry['uml'] = thread_delta(before, after, ticks, vcpus, samples, start_tick)
                    entry['backend_cpu'] = thread_delta(backend_cpu, backend_after, ticks, {})
                    entry['state_samples'] = samples
                    backend_end = ctx.forward('stats')
                    entry['backend'] = {key: backend_end[key] - value for key, value in backend_before.items()
                                        if isinstance(value, int) and not isinstance(value, bool)}
                    entry['kernel'] = counter_delta(kernel_before, inspect())
                    entry['driver_after'] = json.loads(workloads.docker('exec', inspector, '/net-transfer', 'inspect'))
                    entry['complete'] = True
                except Exception as error:
                    entry['error'] = f'{type(error).__name__}: {error}'
                    raise
                finally:
                    with (ctx.run_dir / 'network-transfer-profile.jsonl').open('a') as output:
                        output.write(json.dumps(entry, sort_keys=True) + '\n')
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            proc.stdout.close()
        if proc.returncode:
            raise RuntimeError(f'transfer client exited {proc.returncode}; see {log.name}')
