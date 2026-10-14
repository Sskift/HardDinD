#!/usr/bin/env python3
"""Serial ABBA comparison of immutable virtiofs backends; not product acceptance."""
import argparse
import collections
import hashlib
import json
import pathlib
import statistics
import subprocess
import sys
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--baseline', type=pathlib.Path, required=True)
    p.add_argument('--candidate', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True)
    args = p.parse_args()
    if not args.name or len(args.name) > 20 or not all(c.isalnum() or c in '-_' for c in args.name):
        p.error('invalid comparison name')
    work = args.work.resolve()
    output = work / 'results' / (args.name + '.json')
    runner = pathlib.Path(__file__).with_name('run-shared-locks.py').resolve()
    paths = {k: getattr(args, k).resolve() for k in ('kernel', 'disk', 'baseline', 'candidate')}
    paths.update(probe=work / 'share-probe', runner=runner,
                 runtime=runner.with_name('harddind.py'))
    hashes = {k: sha(path) for k, path in paths.items()}
    if hashes['baseline'] == hashes['candidate']:
        p.error('comparison requires two distinct backend binaries')
    order = ['baseline', 'candidate', 'candidate', 'baseline']
    names = [f'{args.name}-{index}' for index in range(len(order))]
    for name in names:
        if any(path.exists() for path in (work/'instances'/name, work/'shares'/name,
                                         work/'results'/(name+'.json'), work/'results'/(name+'.log'))):
            p.error('refusing existing phase state')
    if output.exists():
        p.error('refusing existing comparison evidence')
    result = {'kind': 'shared_lock_abba_diagnostic', 'full_acceptance': False,
              'analysis_complete': False, 'order': order, 'sha256': hashes, 'phases': [],
              'limitations': ['two phases per backend; repeated samples are not independent hosts',
                              'non-exclusive CPUs and uncontrolled outer host load',
                              'microbenchmark only; functional checks are not rerun in these timing phases',
                              'candidate changes ring-lock scope and uses inline flock dispatch together',
                              'no user-approved performance threshold or full Docker/GPU acceptance']}
    expected_samples = {(mode, phase, sample) for mode, phase in
                        [('native', 'before'), ('docker', 'first'), ('docker', 'second'), ('native', 'after')]
                        for sample in range(3)}
    groups = collections.defaultdict(list)
    try:
        for variant, name in zip(order, names):
            if hashes != {k: sha(path) for k, path in paths.items()}:
                raise ValueError('comparison inputs changed')
            cmd = [sys.executable, str(runner), str(work), '--kernel', str(paths['kernel']),
                   '--disk', str(paths['disk']), '--virtiofsd', str(paths[variant]),
                   '--name', name, '--benchmark-only']
            if variant == 'candidate':
                cmd.append('--inline-flock')
            print(f'phase {name}: {variant}', flush=True)
            started = time.time()
            log_path = work / 'results' / (name + '-driver.log')
            with log_path.open('x') as log:
                rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
            report_path = work / 'results' / (name + '.json')
            row = {'variant': variant, 'name': name, 'started_at': started, 'ended_at': time.time(),
                   'exit_code': rc, 'report': str(report_path)}
            result['phases'].append(row)
            output.write_text(json.dumps(result, indent=2) + '\n')
            report = json.loads(report_path.read_text())
            if (rc or not report.get('benchmark_complete') or not report.get('inputs_unchanged')
                    or report.get('instrumented') or not report.get('benchmark_only')
                    or report.get('inline_flock') != (variant == 'candidate')
                    or report['cpu_affinity'] != [0, 1, 2, 3]
                    or any(v != 'not_tested' for checks in report['checks'].values() for v in checks.values())):
                raise ValueError('phase failed or scope differs')
            for key in ('kernel', 'disk', 'probe', 'runner', 'runtime', variant):
                if report['sha256'].get(str(paths[key])) != hashes[key]:
                    raise ValueError('phase binary or script mismatch')
            samples = report['samples']
            if (len(samples) != len(expected_samples)
                    or {(s['mode'], s['phase'], s['sample']) for s in samples} != expected_samples
                    or any(s['rc'] or s['iterations'] != 1000 or s['seconds'] <= 0 for s in samples)):
                raise ValueError('incomplete or invalid timing samples')
            row['median_seconds'] = report['median_seconds']
            row['host_before'] = report['host_before']
            row['host_after'] = report['host_after']
            row['inline_stats'] = report.get('inline_stats')
            row['report_sha256'] = sha(report_path)
            groups[variant].extend(s['seconds'] for s in samples if s['mode'] == 'docker')
            groups[variant + '_native'].extend(s['seconds'] for s in samples if s['mode'] == 'native')
            output.write_text(json.dumps(result, indent=2) + '\n')
        if hashes != {k: sha(path) for k, path in paths.items()}:
            raise ValueError('comparison inputs changed at completion')
        result['median_seconds'] = {key: statistics.median(values) for key, values in groups.items()}
        result['samples_seconds'] = dict(groups)
        result['candidate_over_baseline'] = result['median_seconds']['candidate'] / result['median_seconds']['baseline']
        result['analysis_complete'] = True
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['analysis_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
