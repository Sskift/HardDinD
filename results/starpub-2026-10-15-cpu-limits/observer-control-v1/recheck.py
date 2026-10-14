#!/usr/bin/env python3
"""Reparse the frozen observer control; no runtime or performance acceptance."""
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REMOTE = '/root/harddind-rebuild/cpu-observer-owner-v1/'


def read(name):
    p = ROOT / name
    if p.exists():
        return p.read_bytes()
    return gzip.decompress(p.with_name(p.name + '.gz').read_bytes())


def check(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    report = json.loads(read('report.json'))
    audit = json.loads(read('cleanup-audit.json'))
    check(report['status'] == 'finished' and report['exit_code'] == 0 and
          report['normal_shutdown'] and report['inputs_unchanged'] and
          not report.get('error') and not report.get('cleanup_error'),
          'incomplete observer experiment')
    check(report['remaining_containers'] == '', 'containers remain')
    check(report['full_acceptance'] is False and report['performance_acceptance'] is False,
          'diagnostic cannot claim acceptance')
    for path, digest in report['sha256'].items():
        if path.startswith(REMOTE) and not path.endswith('/cpu-limit-probe'):
            name = path[len(REMOTE):]
            if name.startswith('run/client/'):
                continue  # Remote binary dependency, hash retained only.
            check(hashlib.sha256(read(name)).hexdigest() == digest, 'source changed: ' + name)
    check(report['sha256'][REMOTE + 'cpu-limit-probe'] ==
          'afef800dc5c107087c896d444d12461865415563cc8e41d06950c86c78d5adaa',
          'different stage-probe binary')
    groups = set()
    commands = {}
    for command in report['commands']:
        label = command['label']
        check(label not in commands, 'duplicate command label')
        commands[label] = command
        cleanup = command['cleanup']
        check(command['exit_code'] == 0 and not command['timed_out'] and
              cleanup['clean_shutdown'] and not cleanup['forced_cleanup'] and
              not cleanup['remaining_live_pids'] and not cleanup['remaining_zombie_pids'],
              'failed or unclean command: ' + label)
        groups.add(str(cleanup['process_group']))
    for key in ('guest_cleanup', 'network_cleanup'):
        cleanup = report['final_state'][key]
        check(cleanup['clean_shutdown'] and not cleanup['forced_cleanup'],
              'unclean instance shutdown: ' + key)
        groups.add(str(cleanup['process_group']))
    check(set(audit['groups']) == groups and audit['all_empty'] and
          all(v == [[], []] for v in audit['groups'].values()),
          'independent process-group audit incomplete')
    check(read('inventory.stdout').strip() == b'', 'raw container inventory is not empty')
    for label in ('busy-inspect-before', 'busy-inspect-after', 'busy-final'):
        objects = json.loads(read(label + '.stdout'))
        check(len(objects) == 1 and objects[0]['Id'] == report['busy_id'],
              'busy container identity changed')
        obj = objects[0]
        check(obj['HostConfig']['NanoCpus'] == 500000000, 'busy quota changed')
        check(obj['State']['Running'] == (label != 'busy-final'), 'unexpected busy lifecycle')
        if label == 'busy-final':
            check(obj['State']['ExitCode'] == 0, 'busy workload failed')
    busy = [json.loads(line) for line in read('busy-logs.stdout').splitlines()]
    check(busy[0]['event'] == 'start' and busy[-1]['event'] == 'end' and
          busy[0]['threads'] == 4 and len(busy[-1]['workers']) == 4,
          'busy worker evidence missing')
    expected = {'quiet-quota', 'quiet-unlimited', 'separate-observer', 'same-observer'}
    check(set(report['cells']) == expected, 'wrong control cells')
    analysis = {'scope': 'one sequential observer-placement diagnostic',
                'full_acceptance': False, 'performance_acceptance': False,
                'model_binary_sha256': report['sha256'][REMOTE + 'cpu-limit-probe'],
                'process_groups_empty': len(groups), 'cells': {}}
    stage_names = ['cpu.max.first', 'cpu.stat', 'cpu.max.second', '/proc/stat', 'process_clock']
    for label, cell in report['cells'].items():
        rows = cell['samples']
        check(len(rows) == 50 and rows == [json.loads(line) for line in
              read(label + '.stdout').splitlines()], 'raw samples disagree: ' + label)
        previous = -1
        quota = label in ('quiet-quota', 'same-observer')
        for row in rows:
            check(row['event'] == 'sample' and row['wall_ns'] > previous and
                  row['cap_end_ns'] >= row['wall_ns'] and
                  row['cap_dur_ns'] == row['cap_end_ns'] - row['wall_ns'],
                  'invalid capture ordering: ' + label)
            previous = row['cap_end_ns']
            check(all(row[key] == 0 for key in ('max_errno', 'max2_errno', 'stat_errno', 'steal_errno'))
                  and row['max_stable'] == 1, 'capture error: ' + label)
            check(row['quota_max'] == row['q2_quota_max'] == int(not quota) and
                  row['quota'] == row['q2_quota'] and row['period'] == row['q2_period'],
                  'inconsistent quota brackets: ' + label)
            if quota:
                check(row['quota'] == 50000 and row['period'] == 100000,
                      'wrong quota in measured container')
            check([s['name'] for s in row['stages']] == stage_names, 'missing stages')
            boundary = row['wall_ns']
            for stage in row['stages']:
                check(boundary <= stage['begin_ns'] <= stage['work_begin_ns'] <=
                      stage['work_end_ns'] <= stage['end_ns'] <= row['cap_end_ns'] and
                      stage['thread_cpu_end_ns'] >= stage['thread_cpu_begin_ns'],
                      'invalid stage timing')
                boundary = stage['end_ns']
        first, last = rows[0], rows[-1]
        if label in ('separate-observer', 'same-observer'):
            check(busy[0]['cap_end_ns'] < first['wall_ns'] < last['cap_end_ns'] <
                  busy[0]['load_deadline_ns'] <= busy[-1]['wall_ns'],
                  'observer did not overlap the actual busy workload')
        worst = max(rows, key=lambda r: r['cap_dur_ns'])
        analysis['cells'][label] = {
            'samples': len(rows), 'elapsed_s': (last['cap_end_ns'] - first['wall_ns']) / 1e9,
            'max_capture_ms': worst['cap_dur_ns'] / 1e6,
            'max_start_to_start_gap_ms': max((y['wall_ns'] - x['wall_ns']) / 1e6
                                            for x, y in zip(rows, rows[1:])),
            'counter_deltas': {k: last[k] - first[k] for k in
                               ('usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec', 'proc_cpu_ns')},
            'worst_capture_stages': worst['stages']}
    print(json.dumps(analysis, indent=2))


if __name__ == '__main__':
    main()
