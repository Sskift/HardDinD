#!/usr/bin/env python3
"""Summarize measured link batching counters without granting acceptance."""
import argparse
import json
import pathlib
import statistics

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('report', type=pathlib.Path)
a = p.parse_args()
d = json.loads(a.report.read_text())
if not d.get('comparison_complete') or len(d['runs']) != len(d['order']):
    raise SystemExit('comparison is incomplete')
summary = {'source': a.report.name, 'full_acceptance': False, 'workloads': {},
           'cpu_note': 'Scheduler CPU includes snapshot intervals; guest CPU covers only tasks present at both snapshots. Changed task identities are counted, not silently imputed.'}
for kind in ['request_reply', 'stream']:
    modes = {'native': [s for r in d['native'] for s in r['samples'][kind]]}
    for batch in sorted(set(d['order'])):
        modes[f'batch_{batch}'] = [s for r in d['runs'] if r['batch'] == batch for s in r['samples'] if s['kind'] == kind]
    result = {}
    for mode, rows in modes.items():
        if not rows: raise SystemExit('missing samples')
        row = {'samples': len(rows), 'median_mbps_each_direction': statistics.median(s['mbps_each_direction'] for s in rows),
               'median_wall_seconds': statistics.median(s['seconds'] for s in rows)}
        if mode != 'native':
            row['median_link'] = {k: statistics.median(s['link'][k] for s in rows) for k in rows[0]['link']}
            row['median_link_syscalls_total'] = statistics.median(s['link']['rx_syscalls']+s['link']['tx_syscalls'] for s in rows)
            row['median_helper_cpu_seconds'] = statistics.median(s['cpu']['helper']['cpu_seconds'] for s in rows)
            row['median_common_guest_task_cpu_seconds'] = statistics.median(s['cpu']['guest']['cpu_seconds'] for s in rows)
            row['samples_with_changed_cpu_task_identity'] = sum(any(s['cpu'][k]['new_tasks'] or s['cpu'][k]['lost_tasks'] for k in ['guest','helper']) for s in rows)
            row['dropped_packets'] = sum(s['link']['dropped_packets'] for s in rows)
        result[mode] = row
    summary['workloads'][kind] = result
print(json.dumps(summary, indent=2))
