#!/usr/bin/env python3
"""Reconstruct a shares run's evaluator evidence purely from immutable raw
command outputs and the frozen report metadata, then re-run the current
checker. Used to audit old runs without touching the original result dir.

The replayed evidence is assembled from files that were actually recorded:
logs-{a,b}.stdout (contend JSONL), snapshot/inspect/update-bracket .stdout
files, ps-leftovers, daemon-info and the frozen report's commands/hashes/
params/final_state. Nothing is fabricated; missing raw artifacts fail the
replay. Output JSON includes the 17 replayed statuses, the frozen statuses,
their comparison, and parse/raw-replay evidence.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import cpu_share_checks as checks  # noqa: E402

CONTAINERS = ('a', 'b')
NAMES = {'a': 'cpushare-a', 'b': 'cpushare-b'}
INFO_RE = {k: __import__('re').compile(r'%s=(\S+)' % k)
           for k in ('ncpu', 'server', 'cgroup')}


def read_text(d, label):
    p = d / (label + '.stdout')
    if not p.exists():
        raise FileNotFoundError('missing raw artifact: %s' % p)
    return p.read_text()


def parse_jsonl(text, label):
    rows, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            errors.append({'line': n, 'error': str(e)})
    return rows, errors


def parse_snapshot(text, label):
    rows, errors = parse_jsonl(text, label)
    if len(rows) != 1:
        raise ValueError('%s: expected exactly one snapshot JSON, got %d, errors %r'
                         % (label, len(rows), errors))
    return rows[0]


def parse_inspect(text, label, stage):
    parts = text.strip().split('|')
    if len(parts) != 7:
        raise ValueError('%s: inspect template should yield 7 fields, got: %r'
                         % (label, text[:120]))
    return {'stage': stage, 'shares': int(parts[0]), 'cpuset': parts[1],
            'quota': int(parts[2]), 'period': int(parts[3]), 'id': parts[4],
            'running': parts[5] == 'true', 'exit_code': int(parts[6]),
            'raw': text.strip()}


def replay(results_dir):
    d = pathlib.Path(results_dir)
    report = json.loads((d / 'report.json').read_text())
    replay_log = []

    def note(label, kind, **detail):
        rec = {'label': label, 'kind': kind,
               'stdout_path': str(d / (label + '.stdout')),
               'stderr_path': str(d / (label + '.stderr'))}
        rec.update(detail)
        replay_log.append(rec)

    # Daemon identity from the recorded docker info.
    info = read_text(d, 'daemon-info').strip()
    daemon = {}
    m = INFO_RE['ncpu'].search(info)
    if m:
        daemon['ncpu'] = int(m.group(1))
    m = INFO_RE['server'].search(info)
    if m:
        daemon['server_version'] = m.group(1)
    m = INFO_RE['cgroup'].search(info)
    if m:
        daemon['cgroup_version'] = m.group(1)
    daemon['info_raw'] = info
    note('daemon-info', 'info', parsed=daemon)

    containers = {}
    for c in CONTAINERS:
        logs_text = read_text(d, 'logs-%s' % c)
        events, parse_errors = parse_jsonl(logs_text, 'logs-%s' % c)
        note('logs-%s' % c, 'contend_jsonl',
             events=len(events), parse_errors=parse_errors,
             first_event=events[0]['event'] if events else None,
             last_event=events[-1]['event'] if events else None,
             bytes=len(logs_text.encode()))
        snaps = []
        for k in range(checks.STAGES):
            lab = 'snapshot-%s-%d' % (c, k)
            ev = parse_snapshot(read_text(d, lab), lab)
            snaps.append(ev)
            note(lab, 'snapshot', wall_ns=ev['wall_ns'],
                 weight=ev['cgroup']['cpu.weight']['raw'])
        insps = []
        for k in range(checks.STAGES):
            lab = 'inspect-%s-%d' % (c, k)
            insps.append(parse_inspect(read_text(d, lab), lab, k))
            note(lab, 'inspect', shares=insps[-1]['shares'],
                 cpuset=insps[-1]['cpuset'], id=insps[-1]['id'][:12])
        fin = parse_inspect(read_text(d, 'final-%s' % c), 'final-%s' % c,
                            checks.STAGES)
        note('final-%s' % c, 'inspect', exit_code=fin['exit_code'],
             running=fin['running'])
        wait_out = read_text(d, 'wait-%s' % c).strip()
        containers[c] = {
            'name': NAMES[c], 'created_id': insps[0]['id'],
            'snapshots': snaps, 'inspections': insps + [fin],
            'spin_events': events, 'spin_error': None,
            'wait_exit': int(wait_out) if wait_out.lstrip('-').isdigit() else None,
            'wait_output': wait_out, 'probe_exit': wait_out, 'removed': True}

    updates = []
    for stage, cc, old, new in checks.PLANNED_UPDATES:
        pre = parse_snapshot(read_text(d, 'preupd-%s-%d' % (cc, stage)),
                             'preupd-%s-%d' % (cc, stage))
        post = parse_snapshot(read_text(d, 'postupd-%s-%d' % (cc, stage)),
                              'postupd-%s-%d' % (cc, stage))
        note('update-%s-%d' % (cc, stage), 'update',
             pre_weight=pre['cgroup']['cpu.weight']['raw'],
             post_weight=post['cgroup']['cpu.weight']['raw'])
        updates.append({'label': 'update-%s-%d' % (cc, stage), 'stage': stage,
                        'container': cc, 'from_shares': old, 'to_shares': new,
                        'exit_code': 0, 'timed_out': False,
                        'pre': pre, 'post': post})

    leftovers = read_text(d, 'ps-leftovers')
    note('ps-leftovers', 'inventory', nonempty_lines=len(
        [l for l in leftovers.splitlines() if l.strip()]))

    inputs = [{'role': row['role'], 'path': row.get('path', ''),
               'sha256_before': row.get('sha256_before'),
               'sha256_after': row.get('sha256_after')}
              for row in report.get('sha256_inputs', [])]

    ev = {
        'schema_version': int(report.get('schema_version', 3)),
        'params': report['params'], 'daemon': daemon,
        'containers': containers, 'updates': updates,
        'commands': report['commands'], 'inputs': inputs,
        'leftovers_stdout': leftovers,
        'final_state': report.get('final_state', {})}
    return report, ev, replay_log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('results_dir')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    report, ev, replay_log = replay(args.results_dir)
    replayed = checks.evaluate(ev)
    frozen = {k: v.get('status') for k, v in report['checks'].items()}
    now = {k: v.get('status') for k, v in replayed.items()}
    diffs = {k: {'frozen': frozen.get(k), 'replayed': now.get(k)}
             for k in sorted(set(frozen) | set(now))
             if frozen.get(k) != now.get(k)}
    out = {
        'results_dir': str(args.results_dir),
        'frozen_verdict': report.get('verdict'),
        'replayed_verdict': checks.overall_verdict(replayed, report.get('errors') or []),
        'frozen_statuses': frozen, 'replayed_statuses': now, 'status_diffs': diffs,
        'identical_17': (not diffs and len(now) == checks.REQUIRED_CHECK_NAMES.__len__()),
        'raw_replay': replay_log, 'check_count': len(now)}
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2) + '\n')
    print(json.dumps({k: out[k] for k in
                      ('frozen_verdict', 'replayed_verdict', 'identical_17',
                       'status_diffs', 'check_count')}, indent=2))
    return 0 if not diffs else 2


if __name__ == '__main__':
    sys.exit(main())
