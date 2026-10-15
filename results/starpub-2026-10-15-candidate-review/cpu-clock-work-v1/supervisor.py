from pathlib import Path
import datetime, fcntl, json, os, signal, subprocess, sys, time, traceback

sys.dont_write_bytecode = True
b = Path(__file__).resolve().parent
w = b.parent
sys.path.insert(0, str(b / 'repo/scripts'))
import harddind
import cpu_limit_checks as checks
from guest_cleanup import cleanup_group, group_members

cells = [('native-before', None, None), ('baseline', 'baseline-kernel', None), ('off-before', 'candidate-kernel', '0'),
         ('on-first', 'candidate-kernel', '1'), ('on-second', 'candidate-kernel', '1'),
         ('off-after', 'candidate-kernel', '0'), ('native-after', None, None)]
r = {'status': 'running', 'stage': 'preflight', 'cells': {}, 'commands': [],
     'full_acceptance': False, 'performance_acceptance': False,
     'resource_enforcement_acceptance': False, 'affinity': [0, 1, 2, 3],
     'limitations': ['Functional busy-load enforcement; no matching native writable cgroup available.',
                    'Clock candidate changes accounting granularity; it has no host steal compensation.',
                    'Existing CFS accounting checker unchanged; no new performance tolerance.',
                    'Native before/after uses the same binary in the existing outer cgroup; no matching native quota group.',
                    'Work counts compare this fixed workload only, not full Docker command performance.',
                    'No GPU experiment, external busy load, host sampler or tracing.',
                    'Candidate source/config linked to the preceding owner clock source reapplication.']}

def save():
    p = b / 'owner-status.tmp'
    p.write_text(json.dumps(r, indent=2) + '\n')
    p.replace(b / 'owner-status.json')

def call(label, cmd, env, timeout):
    row = {'label': label, 'command': cmd, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    r['commands'].append(row)
    save()
    with (b / (label + '.stdout')).open('xb') as out, (b / (label + '.stderr')).open('xb') as err:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
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
    return row

def stop(name, env):
    instance = b / 'run/instances' / name
    if harddind.live(harddind.read_state(instance)):
        call(name + '-final-stop', [sys.executable, str(b / 'repo/scripts/harddind.py'),
             str(b / 'run'), name, 'stop'], env, 100)
    return harddind.read_state(instance)

save()
rc = 1
try:
    os.sched_setaffinity(0, set(r['affinity']))
    prepared = json.loads((b / 'prepare.json').read_text())
    for name, digest in prepared['inputs'].items():
        assert harddind.sha(b / name) == digest, name
    r['inputs'] = prepared['inputs']
    r['external_inputs'] = prepared['external_inputs']
    env = dict(os.environ)
    env.pop('LD_PRELOAD', None)
    env.pop('HARDDIND_HR_CLOCK', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    tested = call('checker-tests', [sys.executable, '-m', 'unittest', 'discover',
                  '-s', str(b / 'repo/tests'), '-p', 'test_cpu_limit_checks.py'], env, 60)
    assert tested['exit_code'] == 0 and not tested.get('timeout')
    for label, kernel, mode in cells:
        name = 'clkwork-' + label
        r['stage'] = label
        cell = r['cells'][label] = {'clock_mode': mode, 'kernel': kernel}
        save()
        cenv = dict(env)
        if mode is not None:
            cenv['HARDDIND_HR_CLOCK'] = mode
        if kernel is None:
            with (w / 'kernel-build.lock').open('a') as lock:
                deadline = time.monotonic() + 180
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('native EX lock 180s')
                        time.sleep(.1)
                row = call(name, [str(b / 'cpu-limit-probe'), 'spin', '--threads', '4',
                                 '--duration-ms', '6000', '--sample-ms', '100'], cenv, 20)
                assert row['exit_code'] == 0 and not row.get('timeout') and row['cleanup']['clean_shutdown']
            events = checks.parse_events((b / (name + '.stdout')).read_text())
            summary = checks.spin_summary(events)
            cell['completed_work'] = summary['completed_work']
            cell['probe_sha256'] = harddind.sha(b / 'cpu-limit-probe')
            cell['completed'] = True
            assert cell['completed_work']['status'] == 'pass'
            save()
            continue
        try:
            row = call(name, [sys.executable, str(b / 'repo/scripts/run-cpu-limits.py'),
                       str(b / 'run'), '--kernel', str(b / kernel), '--disk',
                       str(w / 'runtime-shares-v1.ext4'), '--name', name, '--cpus', '4',
                       '--window-ms', '6000', '--sample-ms', '100'], cenv, 360)
            cell['runner_exit_code'] = row['exit_code']
            report = json.loads((b / 'run/results' / name / 'report.json').read_text())
            cell['verdict'] = report['verdict']
            cell['checks'] = {k: v['status'] for k, v in report['checks'].items()}
            cell['errors'] = report['errors']
            cell['completed_work'] = {k: v['completed_work'] for k, v in report['phases'].items()}
            assert all(v['status'] == 'pass' for v in cell['completed_work'].values())
            cell['probe_sha256'] = harddind.sha(b / 'run/results' / name / 'cpu-limit-probe')
            assert cell['probe_sha256'] == harddind.sha(b / 'cpu-limit-probe'), 'different native/guest binary'

            expected = {'pass': 0, 'fail': 1, 'indeterminate': 2}[report['verdict']]
            assert row['exit_code'] == expected and not row.get('timeout'), label
            assert row['cleanup']['clean_shutdown'], label
        finally:
            st = stop(name, cenv)
            cell['final_state'] = st
            cell['normal_shutdown'] = st.get('phase') == 'stopped' and st.get('guest_exit_code') == 0 and not st.get('forced_shutdown', True)
            save()
        assert cell['normal_shutdown'], label
        cmd = st['command']
        assert 'ncpus=4' in cmd and 'seccomp_threads=1' in cmd
        assert [x for x in cmd if x.startswith('harddind_hr_sched_clock=')] == ([] if mode is None else ['harddind_hr_sched_clock=' + mode])
        assert report['checks']['inputs_unchanged']['status'] == 'pass'
        assert report['checks']['lifecycle_clean']['status'] == 'pass'
        cell['completed'] = True
        save()
    rc = 0
except BaseException:
    r['error'] = traceback.format_exc()
finally:
    try:
        groups = {c['pid'] for c in r['commands'] if 'pid' in c}
        for label, kernel, _ in cells:
            if kernel is None:
                continue
            name = 'clkwork-' + label
            st = stop(name, dict(os.environ))
            if st:
                groups.update(st[k] for k in ['guest_pid', 'network_pid', 'supervisor_pid'] if st.get(k))
            p = b / 'run/results' / name / 'report.json'
            if p.exists():
                report = json.loads(p.read_text())
                groups.update(c['cleanup']['process_group'] for c in report['commands'])
        r['groups'] = {str(g): group_members(g) for g in sorted(groups)}
        r['groups_empty'] = all(v == ([], []) for v in r['groups'].values())
        r['inputs_unchanged'] = all(harddind.sha(b / n) == h for n, h in r.get('inputs', {}).items())
        r['external_inputs_unchanged'] = all(harddind.sha(Path(n)) == h for n, h in r.get('external_inputs', {}).items())
        if not r['groups_empty'] or not r['inputs_unchanged'] or not r['external_inputs_unchanged']:
            rc = 1
    except BaseException:
        r['cleanup_error'] = traceback.format_exc()
        rc = 1
    r.update(status='finished', exit_code=rc, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    save()
sys.exit(rc)
