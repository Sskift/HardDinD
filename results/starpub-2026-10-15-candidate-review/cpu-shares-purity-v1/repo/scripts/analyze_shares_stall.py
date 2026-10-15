#!/usr/bin/env python3
"""Read-only diagnostic: distinguish a SAMPLER/observation stall from actual
workload progress in an archived shares run, using only recorded counters.

A gap with no emitted sample is ambiguous from timestamps alone, but the
bracketed counters separate two cases:

* cgroup `usage_usec` (scheduler accounting for the whole cgroup) and the
  process CLOCK_PROCESS_CPUTIME_ID keep ADVANCING across the gap at a
  substantial rate while no sample is emitted -> the busy workload kept
  progressing and the observation path (sampler descheduled, blocked on a
  syscall/cgroup read) stalled. This does NOT prove shares behavior during
  the gap; the checker correctly quarantines it.
* counters advance at ~zero -> the workload itself stalled/starved.

Caveats, reported honestly: cpu.stat is read INSIDE a bracketed capture, so
within one long capture the read instant is not pinned; only the
counter-delta across the surrounding samples is observable. No quota
information is assumed (runs here are quota-free, max_stable unlimited,
steal_ns 0). Produces no verdict.
"""
import argparse
import json
import pathlib
import sys

GAP_S = 0.5
LONG_CAP_S = 1.0


def load_samples(results_dir, container):
    f = results_dir / ('logs-%s.stdout' % container)
    out = []
    for line in f.read_text(errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get('event') == 'sample':
            out.append(e)
    return out


def analyze(results_dir):
    out = {'results_dir': str(results_dir), 'containers': {},
           'interpretation': {}, 'verdict': None,
           'note': 'read-only stall diagnostic; not a functional verdict'}
    progressing_gaps, quiet_gaps, long_captures = [], [], []
    for c in ('a', 'b'):
        s = load_samples(results_dir, c)
        gaps, lc = [], []
        for i in range(len(s) - 1):
            g = (s[i + 1]['wall_ns'] - s[i]['cap_end_ns']) / 1e9
            if g > GAP_S:
                wall = (s[i + 1]['wall_ns'] - s[i]['wall_ns']) / 1e9
                usage = (s[i + 1]['usage_usec'] - s[i]['usage_usec']) / 1e6
                proc = (s[i + 1]['proc_cpu_ns'] - s[i]['proc_cpu_ns']) / 1e9
                rec = {'after_sample_index': i + 1, 'gap_s': round(g, 3),
                       'stamped_span_s': round(wall, 3),
                       'cgroup_usage_delta_s': round(usage, 4),
                       'process_cpu_delta_s': round(proc, 4),
                       'whole_process_cores_over_span':
                       round(proc / wall, 4) if wall else None,
                       'workload_progressing': proc > 0.1 * wall}
                gaps.append(rec)
                (progressing_gaps if rec['workload_progressing']
                 else quiet_gaps).append({'container': c, **rec})
        for i, e in enumerate(s):
            d = (e['cap_end_ns'] - e['wall_ns']) / 1e9
            if d > LONG_CAP_S:
                # Attribution uses the neighbors: counters read inside the
                # long capture vs the prior/next observations.
                prev = s[i - 1] if i else None
                nxt = s[i + 1] if i + 1 < len(s) else None
                rec = {'sample_index': i, 'wall_s': round(e['wall_ns'] / 1e9, 3),
                       'cap_end_s': round(e['cap_end_ns'] / 1e9, 3),
                       'capture_duration_s': round(d, 3),
                       'weight1': e.get('weight1'), 'weight2': e.get('weight2'),
                       'weight_stable': e.get('weight_stable'),
                       'max_stable': e.get('max_stable'),
                       'steal_ns': e.get('steal_ns'),
                       'sampler_cpu_ns': e.get('sampler_cpu_ns')}
                if prev and nxt:
                    span = (nxt['wall_ns'] - prev['wall_ns']) / 1e9
                    proc = (nxt['proc_cpu_ns'] - prev['proc_cpu_ns']) / 1e9
                    usage = (nxt['usage_usec'] - prev['usage_usec']) / 1e6
                    rec.update({
                        'whole_process_cores_across_capture':
                        round(proc / span, 4) if span else None,
                        'cgroup_usage_across_capture_s': round(usage, 4),
                        'span_to_neighbor_samples_s': round(span, 3),
                        'workload_progressed_during_capture': proc > 0.1 * span})
                lc.append(rec)
                long_captures.append({'container': c, **rec})
        out['containers'][c] = {'samples': len(s), 'gaps_over_0.5_s': gaps,
                                'long_captures_over_1s': lc}
    out['interpretation'] = {
        'progressing_gap_count': len(progressing_gaps),
        'quiet_gap_count': len(quiet_gaps),
        'long_capture_count': len(long_captures),
        'sampler_stall_evidence': progressing_gaps + long_captures,
        'quiet_workload_evidence': quiet_gaps,
        'conclusion': (
            'cgroup/process CPU counters advanced across emission gaps while '
            'no samples were produced: the busy workload kept progressing and '
            'the OBSERVATION path stalled (descheduled/blocked sampler or '
            'cgroup read); the gaps are correctly quarantined and the run '
            'cannot be scored through them'
        ) if progressing_gaps or any(
            x.get('workload_progressed_during_capture') for x in long_captures)
        else 'no progressing counters across gaps; workload itself may have stalled'}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('results_dir')
    ap.add_argument('--out')
    a = ap.parse_args()
    result = analyze(pathlib.Path(a.results_dir).resolve())
    text = json.dumps(result, indent=2)
    if a.out:
        pathlib.Path(a.out).write_text(text + '\n')
    print(json.dumps({
        'progressing_gaps': result['interpretation']['progressing_gap_count'],
        'quiet_gaps': result['interpretation']['quiet_gap_count'],
        'long_captures': result['interpretation']['long_capture_count'],
        'conclusion': result['interpretation']['conclusion']}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
