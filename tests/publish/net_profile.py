"""Read-only host counters around a pipeline network sample."""
import contextlib
import json
import os
import pathlib
import signal
import subprocess
import time


def parse_stat(text):
    pid, rest = text.split(' (', 1)
    comm, fields = rest.rsplit(') ', 1)
    values = fields.split()
    return {'pid': int(pid), 'comm': comm, 'pgrp': int(values[2]),
            'user': int(values[11]), 'system': int(values[12]),
            'start': int(values[19])}


def processes(group, root=pathlib.Path('/proc')):
    result = {}
    for path in root.glob('[0-9]*/stat'):
        try:
            item = parse_stat(path.read_text())
            if item['pgrp'] == group:
                result[(item['pid'], item['start'])] = item
        except (OSError, ValueError, IndexError):
            continue
    return result


def cpu_delta(before, after, ticks):
    """Exited tasks are explicitly incomplete; PID reuse never makes a delta."""
    common = before.keys() & after.keys()
    return {'user_seconds': sum(after[k]['user'] - before[k]['user'] for k in common) / ticks,
            'system_seconds': sum(after[k]['system'] - before[k]['system'] for k in common) / ticks,
            'new_tasks': len(after.keys() - before.keys()),
            'exited_tasks': len(before.keys() - after.keys()),
            'tasks_before': len(before), 'tasks_after': len(after),
            'processes': [{'pid': after[k]['pid'], 'comm': after[k]['comm'],
                           'user_seconds': (after[k]['user'] - before[k]['user']) / ticks,
                           'system_seconds': (after[k]['system'] - before[k]['system']) / ticks}
                          for k in sorted(common)
                          if after[k]['user'] != before[k]['user'] or after[k]['system'] != before[k]['system']],
            'scope': 'tasks alive at both snapshots; excludes exited and new tasks'}


@contextlib.contextmanager
def trace(ctx, metric, side, pair, pid):
    """Opt-in diagnostics perturb timing: never use traced runs for scoring."""
    if os.environ.get('HD_NET_STRACE') != '1' or side != 'guest' or pair != 0:
        yield None
        return
    prefix = ctx.run_dir / (metric + '-strace')
    detail = {'summary': prefix.name + '.txt', 'attached': False}
    with pathlib.Path(str(prefix) + '.stderr').open('w') as log:
        tracer = subprocess.Popen(['strace', '-c', '-f', '-p', str(pid),
                                   '-o', str(prefix) + '.txt'], stdout=subprocess.DEVNULL, stderr=log)
        try:
            time.sleep(0.1)
            status = pathlib.Path(f'/proc/{pid}/status').read_text()
            detail['attached'] = any(line.split() == ['TracerPid:', str(tracer.pid)]
                                     for line in status.splitlines())
            yield detail
        finally:
            if tracer.poll() is None:
                tracer.send_signal(signal.SIGINT)
            try:
                tracer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tracer.kill(); tracer.wait()
            detail['exit_code'] = tracer.returncode


@contextlib.contextmanager
def sample(ctx, metric, side, pair=None):
    """Optional bench hook: with sample(ctx, name, side): run_workload()."""
    state = json.loads((ctx.instance / 'state.json').read_text())
    groups = {key: state[key] for key in ('guest_pid', 'network_pid')}
    before = {key: processes(group) for key, group in groups.items()}
    counters = ctx.forward('stats')
    started = time.monotonic()
    entry = {'metric': metric, 'side': side, 'pair': pair,
             'started': time.time(), 'complete': False}
    entry['affinity'] = {key: sorted(os.sched_getaffinity(pid)) for key, pid in groups.items()}
    entry['network_config'] = {key: counters[key] for key in ('vnet', 'batch_size', 'mtu')}
    try:
        with trace(ctx, metric, side, pair, groups['network_pid']) as tracing:
            entry['trace'] = tracing
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
                 'coalesced', 'user_us', 'system_us')
        entry['network'] = {key: after[key] - counters[key] for key in names if key in counters}
        with (ctx.run_dir / 'network-profile.jsonl').open('a') as output:
            output.write(json.dumps(entry, sort_keys=True) + '\n')
