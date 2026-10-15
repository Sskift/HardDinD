"""Read-only host counters around a pipeline network sample."""
import contextlib
import json
import os
import pathlib
import signal
import subprocess
import threading
import time


def parse_stat(text):
    pid, rest = text.split(' (', 1)
    comm, fields = rest.rsplit(') ', 1)
    values = fields.split()
    return {'pid': int(pid), 'comm': comm, 'pgrp': int(values[2]),
            'user': int(values[11]), 'system': int(values[12]),
            'children_user': int(values[13]), 'children_system': int(values[14]),
            'start': int(values[19])}


def processes(group, root=pathlib.Path('/proc')):
    result = {}
    for path in root.glob('[0-9]*/stat'):
        try:
            item = parse_stat(path.read_text())
            if item['pgrp'] == group:
                status = dict(line.split(':', 1) for line in (path.parent / 'status').read_text().splitlines()
                              if ':' in line)
                item.update(voluntary=int(status.get('voluntary_ctxt_switches', 0)),
                            involuntary=int(status.get('nonvoluntary_ctxt_switches', 0)),
                            tracer=int(status.get('TracerPid', 0)))
                result[(item['pid'], item['start'])] = item
        except (OSError, ValueError, IndexError):
            continue
    return result


def cpu_delta(before, after, ticks):
    """Exited tasks are explicitly incomplete; PID reuse never makes a delta."""
    common = before.keys() & after.keys()
    def group_delta(field):
        total = lambda rows: sum(p[field] + p.get('children_' + field, 0) for p in rows.values())
        return (total(after) - total(before)) / ticks
    return {'user_seconds': sum(after[k]['user'] - before[k]['user'] for k in common) / ticks,
            'system_seconds': sum(after[k]['system'] - before[k]['system'] for k in common) / ticks,
            'group_user_seconds': group_delta('user'), 'group_system_seconds': group_delta('system'),
            'group_scope': 'live process group plus reaped children; assumes children stay in this group',
            'voluntary_switches': sum(after[k].get('voluntary', 0) - before[k].get('voluntary', 0) for k in common),
            'involuntary_switches': sum(after[k].get('involuntary', 0) - before[k].get('involuntary', 0) for k in common),
            'new_tasks': len(after.keys() - before.keys()),
            'exited_tasks': len(before.keys() - after.keys()),
            'tasks_before': len(before), 'tasks_after': len(after),
            'processes': [{'pid': after[k]['pid'], 'comm': after[k]['comm'],
                           'user_seconds': (after[k]['user'] - before[k]['user']) / ticks,
                           'system_seconds': (after[k]['system'] - before[k]['system']) / ticks}
                          for k in sorted(common)
                          if after[k]['user'] != before[k]['user'] or after[k]['system'] != before[k]['system']],
            'scope': 'tasks alive at both snapshots; excludes exited and new tasks'}


def socket_rows(output, pid):
    rows, section = [], []
    for line in [*output.splitlines(), 'END']:
        if line and not line[0].isspace():
            if any(f'pid={pid},' in item for item in section):
                rows.append('\n'.join(section))
            section = []
        section.append(line)
    return rows


@contextlib.contextmanager
def sockets(pid, enabled):
    rows, stopped = [], threading.Event()
    def collect():
        for _ in range(16):
            try:
                result = subprocess.run(['ss', '-tnimp'], capture_output=True, text=True, timeout=2)
                rows.append({'at': time.time(), 'sockets': socket_rows(result.stdout, pid),
                             'error': result.stderr if result.returncode else None})
            except (OSError, subprocess.TimeoutExpired) as error:
                rows.append({'error': str(error)})
                return
            if stopped.wait(0.25):
                return
    thread = threading.Thread(target=collect, daemon=True)
    if enabled:
        thread.start()
    try:
        yield rows
    finally:
        stopped.set()
        if enabled:
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError('socket sampler did not stop before measured pairs')


@contextlib.contextmanager
def trace(ctx, metric, side, pair, pid, *, enabled=False, label='backend', pids=None):
    """Trace guest warmups only; measured pairs must remain untraced."""
    if not (enabled or os.environ.get('HD_NET_STRACE') == '1') or side != 'guest' or pair is not None:
        yield None
        return
    prefix = ctx.run_dir / (metric + '-' + label + '-strace')
    pids = [pid] if pids is None else pids
    detail = {'summary': prefix.name + '.txt', 'stderr': prefix.name + '.stderr',
              'requested_pids': pids, 'attached': []}
    if not pids:
        detail['error'] = 'no untraced processes available'
        yield detail
        return
    with pathlib.Path(str(prefix) + '.stderr').open('w') as log:
        follow = ['-f'] if label == 'backend' else []
        tracer = subprocess.Popen(['strace', '-c', *follow, *[arg for p in pids for arg in ('-p', str(p))],
                                   '-o', str(prefix) + '.txt'], stdout=subprocess.DEVNULL, stderr=log)
        try:
            time.sleep(0.1)
            for p in pids:
                try:
                    status = pathlib.Path(f'/proc/{p}/status').read_text()
                    if any(line.split() == ['TracerPid:', str(tracer.pid)] for line in status.splitlines()):
                        detail['attached'].append(p)
                except FileNotFoundError:
                    pass
            if not detail['attached']:
                detail['error'] = 'strace attached no requested process; see stderr'
            yield detail
        finally:
            if tracer.poll() is None:
                tracer.send_signal(signal.SIGINT)
            try:
                tracer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tracer.kill(); tracer.wait()
            detail['exit_code'] = tracer.returncode
            detail['detached'] = not any(item['tracer'] == tracer.pid for item in processes(pid).values())
            if not detail['detached']:
                raise RuntimeError('network tracer remains attached before measured pairs')


@contextlib.contextmanager
def sample(ctx, metric, side, pair=None, trace_warmup=False):
    """Optional bench hook: with sample(ctx, name, side): run_workload()."""
    state = json.loads((ctx.instance / 'state.json').read_text())
    groups = {key: state[key] for key in ('guest_pid', 'network_pid')}
    before = {key: processes(group) for key, group in groups.items()}
    counters = ctx.forward('stats')
    started = time.monotonic()
    entry = {'metric': metric, 'side': side, 'pair': pair,
             'started': time.time(), 'complete': False}
    entry['affinity'] = {key: sorted(os.sched_getaffinity(pid)) for key, pid in groups.items()}
    entry['network_config'] = {key: counters[key] for key in
                              ('vnet', 'batch_size', 'mtu', 'link_sndbuf', 'link_rcvbuf') if key in counters}
    try:
        with contextlib.ExitStack() as stack:
            entry['sockets'] = stack.enter_context(sockets(groups['network_pid'],
                trace_warmup and side == 'guest' and pair is None))
            entry['trace'] = {}
            for key, label in (('network_pid', 'backend'), ('guest_pid', 'uml')):
                available = [p['pid'] for p in before[key].values() if not p['tracer']]
                entry['trace'][label] = stack.enter_context(trace(ctx, metric, side, pair, groups[key],
                    enabled=trace_warmup, label=label, pids=available))
            yield entry
            entry['complete'] = True
    finally:
        entry['wall_seconds'] = time.monotonic() - started
        ticks = os.sysconf('SC_CLK_TCK')
        entry['cpu'] = {key: cpu_delta(before[key], processes(group), ticks)
                        for key, group in groups.items()}
        after = ctx.forward('stats')
        names = ('rx_packets', 'tx_packets', 'rx_bytes', 'tx_bytes', 'dropped_packets',
                 'rx_syscalls', 'tx_syscalls', 'poll_calls', 'rx_gso', 'tx_gso',
                 'coalesced', 'coalesced_acks', 'rx_csum_packets', 'rx_csum_bytes',
                 'slirp_gso', 'slirp_gso_bytes', 'user_us', 'system_us')
        entry['network'] = {key: after[key] - counters[key] for key in names if key in counters}
        with (ctx.run_dir / 'network-profile.jsonl').open('a') as output:
            output.write(json.dumps(entry, sort_keys=True) + '\n')


def wrap(ctx, metric, side, fn, trace_warmup=False):
    """Wrap a pipeline sample; the first call is warmup, then numbered pairs."""
    calls = 0
    def run():
        nonlocal calls
        pair = None if calls == 0 else calls - 1
        calls += 1
        with sample(ctx, metric, side, pair, trace_warmup) as entry:
            output = fn()
            entry['workload'] = json.loads(output.strip().splitlines()[-1])
            return output
    return run
