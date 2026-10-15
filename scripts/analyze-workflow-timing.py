#!/usr/bin/env python3
"""Combine workflow timing reports for controlled kernel comparison.

This is *not* a native-performance judgment and applies no threshold. It joins
several run-workflow-timing.py reports that must share every fixed input
except the kernel, keeps observations separate (including failures), and emits
per-stage median/min/max latency grouped by kernel sha. At least three
successful observations per workflow are required for that workflow to be
marked repeated; failures, timeouts and never-run observations are counted
and never silently dropped.
"""
import argparse
import json
import pathlib
import statistics
import sys


FIXED_INPUT_KEYS = ('rootfs', 'disk', 'payload_one_mib', 'payload_64kib')


def median(values):
    return statistics.median(values) if values else None


def load_reports(paths):
    reports = []
    for path in paths:
        data = json.loads(pathlib.Path(path).read_text())
        if data.get('kind') != 'docker_workflow_timing':
            raise ValueError('not a docker_workflow_timing report: ' + str(path))
        reports.append(data)
    return reports


def fixed_inputs(report):
    inputs = report['inputs']
    return {'inputs': {key: {k: v for k, v in inputs.get(key, {}).items() if k not in ('data', 'path')}
                       for key in FIXED_INPUT_KEYS},
            'sources': inputs.get('sources'), 'config': report.get('config'),
            'cpu_affinity': report.get('host', {}).get('cpu_affinity'),
            'server_version': report.get('server_version')}



def compare(reports):
    if not reports: raise ValueError('no reports')
    baseline = reports[0]
    base_inputs = fixed_inputs(baseline)
    mismatches = []
    for report in reports[1:]:
        if fixed_inputs(report) != base_inputs:
            mismatches.append(report['experiment'])
    kernels = {}
    for report in reports:
        kernel_sha = report['inputs']['kernel']['sha256']
        entry = kernels.setdefault(kernel_sha, {'kernel_sha256': kernel_sha,
                                                'experiments': [],
                                                'workflows': {}})
        entry['experiments'].append(report['experiment'])
        for wf_name, data in report['workflows'].items():
            bucket = entry['workflows'].setdefault(wf_name, {
                'observations': {'pass': 0, 'fail': 0, 'timeout': 0,
                                 'incomplete_fail': 0, 'not_run_wall_budget': 0},
                'problems': [], 'stages': {}, 'observation_body_seconds': []})
            for obs in data['observations']:
                bucket['observations'][obs['status']] = \
                    bucket['observations'].get(obs['status'], 0) + 1
                if obs['status'] != 'pass':
                    bucket['problems'].append({'experiment': report['experiment'],
                                               'rep': obs['rep'], 'status': obs['status'],
                                               'error': obs.get('error')})
                if obs['status'] != 'not_run_wall_budget':
                    bucket['observation_body_seconds'].append(obs['body_seconds'])
                for comp in obs['components']:
                    if comp.get('synthetic'):
                        continue
                    bucket['stages'].setdefault(comp['stage'], []).append(comp['seconds'])
    for entry in kernels.values():
        for bucket in entry['workflows'].values():
            bucket['stage_seconds'] = {
                stage: {'n': len(values), 'min_seconds': min(values),
                        'median_seconds': median(values), 'max_seconds': max(values)}
                for stage, values in sorted(bucket.pop('stages').items())}
            bodies = bucket.pop('observation_body_seconds')
            bucket['observation_body_seconds'] = {
                'n': len(bodies), 'min_seconds': min(bodies) if bodies else None,
                'median_seconds': median(bodies), 'max_seconds': max(bodies) if bodies else None}
            bucket['repeated_observations'] = bucket['observations']['pass'] >= 3
    return {
        'kind': 'docker_workflow_timing_kernel_comparison',
        'native_docker_comparison': 'not_tested',
        'native_docker_comparison_reason': baseline.get('native_docker_comparison_reason'),
        'near_native_acceptance': False,
        'performance_threshold_enforced': False,
        'performance_note': 'median latencies per kernel only; no threshold or ranking is judged here',
        'report_count': len(reports),
        'fixed_inputs_match_except_kernel': not mismatches,
        'fixed_input_mismatch_experiments': mismatches,
        'shards': sorted({shard for report in reports for shard in report['shards']}),
        'reps_requested_values': sorted({report['reps_requested'] for report in reports}),
        'kernels': sorted(kernels.values(), key=lambda item: item['kernel_sha256']),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('reports', nargs='+', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('refusing to overwrite ' + str(args.output))
    reports = load_reports(args.reports)
    result = compare(reports)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'kernels': len(result['kernels']),
                      'fixed_inputs_match_except_kernel':
                          result['fixed_inputs_match_except_kernel'],
                      'native_docker_comparison': 'not_tested',
                      'near_native_acceptance': False}, indent=2))
    return 0 if result['fixed_inputs_match_except_kernel'] else 1


if __name__ == '__main__':
    sys.exit(main())
