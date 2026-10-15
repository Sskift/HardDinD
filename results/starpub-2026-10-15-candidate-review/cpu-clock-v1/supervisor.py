from pathlib import Path
import datetime, fcntl, importlib.util, json, os, signal, statistics, subprocess, sys, time, traceback

b = Path(__file__).resolve().parent
w = b.parent
sys.path.insert(0, str(b / 'repo/scripts'))
import harddind
from guest_cleanup import cleanup_group, group_members
spec = importlib.util.spec_from_file_location('witness_check', b / 'host_correlate.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
cells = [('native-before', None, None), ('baseline', b / 'baseline-kernel', None),
         ('off-before', b / 'candidate-kernel', '0'), ('on-first', b / 'candidate-kernel', '1'),
         ('on-second', b / 'candidate-kernel', '1'), ('off-after', b / 'candidate-kernel', '0'),
         ('native-after', None, None)]
r = {'status': 'running', 'stage': 'preflight', 'commands': [], 'cells': {}, 'states': {},
     'diagnostic_only': True, 'full_acceptance': False, 'performance_acceptance': False,
     'resource_enforcement_acceptance': False, 'affinity': [0, 1, 2, 3],
     'limitations': ['Quiet observer only; no busy-load resource enforcement conclusion.',
                    'No external host sampler or tracing; background host scheduling remains uncontrolled.',
                    'Native witness uses existing outer cgroup; no matching native 0.5 CPU group is available.',
                    'Clock candidate changes scheduling/accounting granularity and has no steal compensation.',
                    'Author-built candidate; owner independently reapplied source and checked config.']}

def save():
    p = b / 'owner-status.tmp'
    p.write_text(json.dumps(r, indent=2) + '\n')
    p.replace(b / 'owner-status.json')

def lock(mode):
    f = (w / 'kernel-build.lock').open('a')
    end = time.monotonic() + 300
    while True:
        try:
            fcntl.flock(f, mode | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() >= end:
                f.close()
                raise TimeoutError('lock wait 300s')
            time.sleep(.25)

def call(label, command, env, timeout=180, required=True):
    row = {'label': label, 'command': command, 'clock_mode': env.get('HARDDIND_HR_CLOCK'),
           'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    r['commands'].append(row)
    save()
    with (b / (label + '.stdout')).open('xb') as out, (b / (label + '.stderr')).open('xb') as err:
        p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             start_new_session=True, env=env)
        row['pid'] = p.pid
        save()
        start = time.monotonic()
        try:
            row['exit_code'] = p.wait(timeout)
        except subprocess.TimeoutExpired:
            row['timeout'] = True
            os.killpg(p.pid, signal.SIGKILL)
            row['exit_code'] = p.wait(15)
        finally:
            row['wall_seconds'] = time.monotonic() - start
            row['cleanup'] = cleanup_group(p.pid)
            save()
    if required:
        assert row['exit_code'] == 0 and not row.get('timeout') and row['cleanup']['clean_shutdown'], label
    return row

def command(name):
    return [sys.executable, str(b / 'repo/scripts/harddind.py'), str(b / 'run'), name]

def host_state():
    return {str(p): p.read_text() for p in map(Path, ['/proc/loadavg', '/sys/fs/cgroup/cpu.stat',
            '/sys/fs/cgroup/cpu.max', '/sys/fs/cgroup/cpu.pressure'])}

def analyze(label, quota):
    rows = [json.loads(line) for line in (b / (label + '.stdout')).read_text().splitlines()]
    summaries = [json.loads(line) for line in (b / (label + '.stderr')).read_text().splitlines()]
    syncs = [x for x in rows if x['event'] == 'sync']
    caps = [x for x in rows if x['event'] == 'cap']
    assert len(rows) == 62 and len(summaries) == 1
    problems = checker.validate_witness(syncs, caps, summaries[0], 60)
    assert not problems, problems
    assert summaries[0]['status'] == 'complete'
    for c in caps:
        assert c['max_stable'] == 1 and c['stat']['present'] == 1
        assert all(c[key]['errno'] == 0 and c[key]['len'] > 0 for key in ['max1', 'max2', 'stat', 'proc'])
        assert c['mono_begin_ns'] == c['stages'][0]['begin_ns']
        assert all(v['begin_ns'] >= u['end_ns'] for u, v in zip(c['stages'], c['stages'][1:]))
    assert all(v['mono_begin_ns'] >= u['stages'][-1]['end_ns'] for u, v in zip(caps, caps[1:]))
    if quota is not None:
        for c in caps:
            for key in ['max1', 'max2']:
                assert c[key]['raw'].split() == (['50000', '100000'] if quota else ['max', '100000']), c[key]
    times = [c['stages'][-1]['end_ns'] - c['mono_begin_ns'] for c in caps]
    gaps = [v['mono_begin_ns'] - u['mono_begin_ns'] for u, v in zip(caps, caps[1:])]
    def desc(values):
        ordered = sorted(values)
        return {'count': len(values), 'median_ms': statistics.median(values) / 1e6,
                'p95_ms': ordered[int((len(ordered)-1)*.95)] / 1e6, 'max_ms': max(values) / 1e6}
    return {'summary': summaries[0], 'capture': desc(times), 'inter_capture': desc(gaps),
            'worst_capture': max(caps, key=lambda x: x['stages'][-1]['end_ns'] - x['mono_begin_ns'])}

save()
rc = 1
try:
    os.sched_setaffinity(0, set(r['affinity']))
    prepared = json.loads((b / 'prepare.json').read_text())
    assert prepared['source_reapplication_matches'] and prepared['config_identical']
    for name, digest in prepared['files'].items():
        assert harddind.sha(b / name) == digest, name
    r['inputs'] = prepared['files']
    r['external_inputs'] = {str(p): harddind.sha(p) for p in [w / 'runtime-shares-v1.ext4',
        w / 'network-backend', w / 'client/docker', w / 'client/ld-musl-x86_64.so.1']}
    for label, kernel, mode in cells:
        name = 'clock-' + label
        base = command(name)
        env = dict(os.environ)
        env.pop('LD_PRELOAD', None)
        env.pop('HARDDIND_HR_CLOCK', None)
        if mode is not None:
            env['HARDDIND_HR_CLOCK'] = mode
        cell = r['cells'][label] = {'mode': mode, 'host_before': host_state(), 'samples': {}}
        r['stage'] = label
        save()
        with lock(fcntl.LOCK_SH if kernel else fcntl.LOCK_EX):
            try:
                if kernel:
                    call(label + '-start', base + ['start', '--kernel', str(kernel), '--disk',
                         str(w / 'runtime-shares-v1.ext4'), '--cpus', '4'], env)
                    cell['ready_state'] = harddind.read_state(b / 'run/instances' / name)
                    cmd = cell['ready_state']['command']
                    assert 'ncpus=4' in cmd and 'seccomp_threads=1' in cmd
                    assert [x for x in cmd if x.startswith('harddind_hr_sched_clock=')] == ([] if mode is None else ['harddind_hr_sched_clock=' + mode])
                    call(label + '-import', base + ['docker', 'import', str(b / 'rootfs.tar'), 'harddind-clock-witness:local'], env)
                for workload in (['quota', 'unlimited'] if kernel else ['unlimited']):
                    sample = label + '-' + workload
                    args = ['witness', '--count', '60', '--interval-ms', '100']
                    if kernel:
                        cmd = base + ['docker', 'run', '--rm', '--name', name + '-probe']
                        if workload == 'quota':
                            cmd += ['--cpus', '0.5']
                        cmd += ['harddind-clock-witness:local', '/cpu-capture-witness'] + args
                    else:
                        cmd = [str(b / 'cpu-capture-witness')] + args
                    call(sample, cmd, env, 60)
                    cell['samples'][workload] = analyze(sample, workload == 'quota' if kernel else None)
                    save()
            finally:
                if kernel:
                    st = harddind.read_state(b / 'run/instances' / name)
                    if harddind.live(st) and st.get('phase') == 'ready':
                        call(label + '-inventory', base + ['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '-probe$'], env, 30)
                        if (b / (label + '-inventory.stdout')).read_text().strip():
                            call(label + '-remove', base + ['docker', 'rm', '-f', name + '-probe'], env, 30, False)
                    if harddind.live(harddind.read_state(b / 'run/instances' / name)):
                        call(label + '-stop', base + ['stop'], env, required=False)
                    st = harddind.read_state(b / 'run/instances' / name)
                    r['states'][name] = st
                    cell['normal_shutdown'] = st.get('phase') == 'stopped' and st.get('guest_exit_code') == 0 and not st.get('forced_shutdown', True)
                    save()
                    assert cell['normal_shutdown'], label
        cell['host_after'] = host_state()
        cell['completed'] = True
        save()
    rc = 0
except BaseException:
    r['error'] = traceback.format_exc()
finally:
    try:
        for label, kernel, mode in cells:
            if not kernel:
                continue
            name = 'clock-' + label
            st = harddind.read_state(b / 'run/instances' / name)
            if harddind.live(st):
                call(label + '-final-stop', command(name) + ['stop'], dict(os.environ), required=False)
                st = harddind.read_state(b / 'run/instances' / name)
            if st:
                r['states'][name] = st
        groups = {c['pid'] for c in r['commands'] if 'pid' in c}
        for st in r['states'].values():
            groups.update(st[k] for k in ['guest_pid', 'network_pid', 'supervisor_pid'] if st.get(k))
        r['groups'] = {str(g): group_members(g) for g in sorted(groups)}
        r['groups_empty'] = all(v == ([], []) for v in r['groups'].values())
        r['inputs_unchanged'] = bool(r.get('inputs')) and all(harddind.sha(b / n) == h for n, h in r['inputs'].items())
        r['external_inputs_unchanged'] = bool(r.get('external_inputs')) and all(harddind.sha(Path(n)) == h for n, h in r['external_inputs'].items())
        if not r['groups_empty'] or not r['inputs_unchanged'] or not r['external_inputs_unchanged']:
            rc = 1
    except BaseException:
        r['cleanup_error'] = traceback.format_exc()
        rc = 1
    r.update(status='finished', exit_code=rc, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    save()
sys.exit(rc)
