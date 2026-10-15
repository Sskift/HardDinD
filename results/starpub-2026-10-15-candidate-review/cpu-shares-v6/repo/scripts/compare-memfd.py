#!/usr/bin/env python3
"""Separate RAM and executable backing costs on one kernel; retain failed runs."""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
import shutil

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('work', type=pathlib.Path)
p.add_argument('--kernel', type=pathlib.Path, required=True)
p.add_argument('--name', required=True)
p.add_argument('--pairwise', action='store_true', help='bracket each file/both guest with native samples')
a = p.parse_args()
if not a.name or not all(c.isalnum() or c in '-_' for c in a.name): p.error('invalid name')
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
os.sched_setaffinity(0, [0, 1, 2, 3])
lock = (work / 'kernel-build.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_SH)
output = work / 'results'
summary_path = output / (a.name + '-comparison.json')
if summary_path.exists(): raise RuntimeError('refusing to overwrite comparison')
paths = {'kernel': a.kernel.resolve(), 'disk': work / 'mapping.ext4',
         'core': work / 'probe-root/corebench', 'dispatch': work / 'dispatch-root/dispatchbench',
         'runner': repo / 'scripts/run-core.py', 'checks': repo / 'scripts/result_checks.py',
         'comparison': pathlib.Path(__file__).resolve()}
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
hashes = {k:sha(v) for k,v in paths.items()}
cases = [('syscall',1),('exec',1),('threads',1),('threads',4),('processes',1),('processes',4),
         ('identity',1),('fdio',1),('mapping',1),('thread_syscalls',4),('isolation',1),('parallel_exec',4)]
modes = {'file': (False,False), 'ram': (True,False), 'stub': (False,True), 'both': (True,True)}
order = (['native','file','native','both','native','both','native','file','native'] if a.pairwise
         else ['native','file','ram','stub','both','both','stub','ram','file','native'])
groups = {k:[] for k in dict.fromkeys(order)}
expected_samples = {k:order.count(k)*3 for k in groups}
sources = output / (a.name + '-sources'); sources.mkdir(exist_ok=False)
for key in ['runner','checks','comparison']: shutil.copy2(paths[key], sources / paths[key].name)
summary = {'kind':'memfd_backing_microbench_only','sha256':hashes,'kernel':str(paths['kernel']),
           'host_cpu_affinity':sorted(os.sched_getaffinity(0)),'guest_cpus':4,'guest_memory':'2G','spin':256,
           'pairwise':a.pairwise,'expected_samples_per_case':expected_samples,'order':[],'failed_runs':[],'comparison_complete':False,'full_performance_acceptance':False,
           'threshold':None,'limitations':['CPUs pinned but not exclusive; shared host load remains',
                                          'microbenchmarks do not establish Docker, network or GPU performance parity']}
def host_state():
    row={'time_unix':time.time()}
    for name,path in {'cpu_stat':'/sys/fs/cgroup/cpu.stat','cpu_pressure':'/sys/fs/cgroup/cpu.pressure',
                      'cpu_max':'/sys/fs/cgroup/cpu.max','memory_events':'/sys/fs/cgroup/memory.events',
                      'memory_stat':'/sys/fs/cgroup/memory.stat','io_stat':'/sys/fs/cgroup/io.stat',
                      'loadavg':'/proc/loadavg'}.items():
        try:row[name]=pathlib.Path(path).read_text()
        except OSError as error:row[name]={'error':str(error)}
    return row
def save():summary_path.write_text(json.dumps(summary,indent=2)+'\n')
try:
    for index,mode in enumerate(order):
        name=f'{a.name}-{index}-{mode}'; path=output/(name+'.json')
        if path.exists() or path.with_suffix('.log').exists(): raise RuntimeError('refusing existing phase')
        entry={'mode':mode,'file':path.name,'host_before':host_state()}
        summary['order'].append(entry);save()
        rows=[]
        if mode=='native':
            for case_index,(test,workers) in enumerate(cases):
                for _ in range(3):
                    command=[str(paths['core']),test,str(workers)] if case_index<6 else [str(paths['dispatch']),test]
                    row=json.loads(subprocess.check_output(command,text=True,timeout=90))
                    if (row['test'],row['workers'])!=(test,workers) or (case_index>=6 and row.get('verified') is not True):
                        raise RuntimeError('native result mismatch')
                    rows.append(row)
            path.write_text(json.dumps({'kind':'native_microbench','measurements':rows,'sha256':hashes,
                                        'host_cpu_affinity':sorted(os.sched_getaffinity(0))},indent=2)+'\n')
            valid=True
        else:
            ram,stub=modes[mode]
            command=[sys.executable,str(paths['runner']),str(work),'--kernel',str(paths['kernel']),
                     '--threads-experiment','--broker-cache','--defer-ipi','--mapping-semantics','--dispatch-bench',
                     '--disk',str(paths['disk']),'--cpus','4','--spin','256','--timeout','300','--result-name',name]
            if ram:command.append('--memfd-ram')
            if stub:command.append('--memfd-stub')
            entry['command']=command;save()
            rc=subprocess.run(command,stdout=subprocess.DEVNULL).returncode
            report=json.loads(path.read_text())
            backing=report.get('memory_backing',{})
            valid=(rc==0 and report['passed'] and report['kernel_sha256']==hashes['kernel']
                   and report['benchmark_sha256']==hashes['core'] and report['dispatch_benchmark_sha256']==hashes['dispatch']
                   and backing.get('verified') and backing.get('stub_backend')==('memfd' if stub else 'file')
                   and report['mapping_semantics'] and report['thread_semantics'])
            entry['memory_backing']=backing
            if valid:rows=report['measurements']
        if not valid:summary['failed_runs'].append(path.name)
        groups[mode].extend(rows)
        entry['status']='pass' if valid else 'fail';entry['host_after']=host_state();save()
        print(name+': '+entry['status'],flush=True)
    if hashes!={k:sha(v) for k,v in paths.items()}:raise RuntimeError('inputs changed')
    comparisons=[]
    for test,workers in cases:
        samples={m:[r['seconds'] for r in rows if (r['test'],r['workers'])==(test,workers)] for m,rows in groups.items()}
        if any(len(v)>expected_samples[m] or any(t<=0 for t in v) for m,v in samples.items()):raise RuntimeError('invalid samples')
        median={m:statistics.median(v) if len(v)==expected_samples[m] else None for m,v in samples.items()}
        comparisons.append({'test':test,'workers':workers,'median_seconds':median,'samples_seconds':samples,
                            'file_over_both':median['file']/median['both'] if median['file'] and median['both'] else None,
                            'both_over_native':median['both']/median['native'] if median['both'] and median['native'] else None})
    summary['comparison']=comparisons
    if a.pairwise and not summary['failed_runs']:
        normalized=[]
        for test,workers in cases:
            per_run=[]
            for index,mode in enumerate(order):
                if mode=='native':continue
                def median_at(position):
                    report=json.loads((output/summary['order'][position]['file']).read_text())
                    values=[r['seconds'] for r in report['measurements'] if (r['test'],r['workers'])==(test,workers)]
                    if len(values)!=3:raise RuntimeError('incomplete bracketing samples')
                    return statistics.median(values)
                native=(median_at(index-1)+median_at(index+1))/2
                per_run.append({'mode':mode,'guest_file':summary['order'][index]['file'],
                                'bracketing_native_seconds':native,'guest_over_native':median_at(index)/native})
            medians={m:statistics.median(r['guest_over_native'] for r in per_run if r['mode']==m) for m in ['file','both']}
            normalized.append({'test':test,'workers':workers,'runs':per_run,'median_guest_over_native':medians,
                               'normalized_file_over_both':medians['file']/medians['both']})
        summary['bracket_normalized']=normalized
    summary['comparison_complete']=not summary['failed_runs'] and all(all(v is not None for v in r['median_seconds'].values()) for r in comparisons)
except (Exception,KeyboardInterrupt) as error:
    summary['error']=type(error).__name__+': '+str(error)
finally:save()
print(json.dumps({'report':str(summary_path),'comparison_complete':summary['comparison_complete'],
                  'failed_runs':summary['failed_runs'],'error':summary.get('error')},indent=2))
sys.exit(0 if summary['comparison_complete'] else 1)
