from pathlib import Path
import datetime, fcntl, json, os, signal, statistics, subprocess, sys, time, traceback

b = Path(__file__).resolve().parent
w = b.parent
repo = b / 'repo'
work = b / 'run'
sys.path.insert(0, str(repo / 'scripts'))
import harddind
from guest_cleanup import cleanup_group, group_members

cells = [('native-before', None, None), ('baseline', b / 'baseline-kernel', None),
         ('candidate-off', b / 'candidate-kernel', '0'),
         ('candidate-on', b / 'candidate-kernel', '4'),
         ('candidate-off-after', b / 'candidate-kernel', '0'),
         ('native-after', None, None)]
fields = ['cold_ns', 'warm_ns', 'cold_minor', 'warm_minor', 'cold_major', 'warm_major']
r = {'status': 'running', 'stage': 'prepare', 'commands': [], 'cells': {},
     'full_acceptance': False, 'performance_acceptance': False,
     'semantic_acceptance': False, 'diagnostic_only': True,
     'limitations': ['Sequential exploratory microbenchmark, not Docker or application performance acceptance.',
                    'Opt-in faultaround changes residency, RSS, mincore and OOM timing; semantic tests remain pending.',
                    'Candidate counter helpers still read clocks when counters are disabled; off controls retain this cost.',
                    'Owner verified patch reapplication and frozen source/config binding; kernel is author build6, not an independent owner rebuild.'],
     'affinity': [0, 1, 2, 3], 'states': {}}


def save():
    tmp = b / 'owner-status.tmp'
    tmp.write_text(json.dumps(r, indent=2) + '\n')
    tmp.replace(b / 'owner-status.json')


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
                raise TimeoutError('global lock wait 300 seconds')
            time.sleep(.25)


def call(label, command, env, timeout=180, required=True):
    row = {'label': label, 'command': command,
           'faultaround_env': env.get('HARDDIND_FAULTAROUND_ORDER'),
           'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    r['commands'].append(row)
    save()
    with (b / (label + '.stdout')).open('xb') as out, (b / (label + '.stderr')).open('xb') as err:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                env=env, start_new_session=True)
        row['pid'] = proc.pid
        save()
        start = time.monotonic()
        try:
            row['exit_code'] = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            row['timeout'] = True
            os.killpg(proc.pid, signal.SIGKILL)
            row['exit_code'] = proc.wait(15)
        finally:
            row['wall_seconds'] = time.monotonic() - start
            row['cleanup'] = cleanup_group(proc.pid)
            save()
    if required:
        assert row['exit_code'] == 0 and not row.get('timeout') and row['cleanup']['clean_shutdown'], label
    return row


def command(name):
    return [sys.executable, str(repo / 'scripts/harddind.py'), str(work), name]


def host_state():
    return {str(p): p.read_text() for p in map(Path, ['/proc/loadavg', '/sys/fs/cgroup/cpu.stat',
            '/sys/fs/cgroup/cpu.max', '/sys/fs/cgroup/cpu.pressure'])}


save()
rc = 1
try:
    os.sched_setaffinity(0, set(r['affinity']))
    binding = json.loads((b / 'kernel-binding.json').read_text())
    assert binding['patch_reapplication_matches'] and binding['config_identical']
    r['inputs'] = {str(p): harddind.sha(p) for p in [b / 'supervisor.py', b / 'kernel-binding.json',
        b / 'baseline-kernel', b / 'candidate-kernel', b / 'cpu-touch-probe', b / 'rootfs.tar',
        w / 'runtime-shares-v1.ext4', w / 'network-backend', w / 'client/docker', w / 'client/ld-musl-x86_64.so.1']}
    r['sources'] = {str(p.relative_to(b)): harddind.sha(p) for folder in ['repo', 'source', 'reapplied', 'author-evidence']
                    for p in (b / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    assert r['inputs'][str(b / 'candidate-kernel')] == binding['candidate_kernel_sha256']
    assert r['inputs'][str(b / 'baseline-kernel')] == binding['baseline_kernel_sha256']
    save()
    for label, kernel, order in cells:
        name = 'fa-' + label
        env = dict(os.environ)
        env.pop('LD_PRELOAD', None)
        env.pop('HARDDIND_FAULTAROUND_ORDER', None)
        if order is not None:
            env['HARDDIND_FAULTAROUND_ORDER'] = order
        base = command(name)
        instance = work / 'instances' / name
        r['stage'] = label
        cell = r['cells'][label] = {'order': order, 'host_before': host_state(), 'samples': []}
        save()
        with lock(fcntl.LOCK_SH if kernel else fcntl.LOCK_EX):
            try:
                if kernel:
                    call(label + '-start', base + ['start', '--kernel', str(kernel), '--disk',
                         str(w / 'runtime-shares-v1.ext4'), '--cpus', '4', '--gpu-control-experiment',
                         '--gpu-async-wait', '--memfd-ram', '--memfd-stub'], env)
                    cell['ready_state'] = harddind.read_state(instance)
                    actual = cell['ready_state']['command']
                    assert 'ncpus=4' in actual and 'gpu_async_wait=1' in actual
                    assert [x for x in actual if x.startswith('uml_faultaround=')] == ([] if order is None else ['uml_faultaround=' + order])
                    assert [x for x in actual if x.startswith('uml_faultstat=')] == ([] if order is None else ['uml_faultstat=0'])
                    call(label + '-import', base + ['docker', 'import', str(b / 'rootfs.tar'), 'harddind-cpu-touch:local'], env)
                for rep in range(3):
                    sample = f'{label}-{rep}'
                    cmd = [str(b / 'cpu-touch-probe')] if kernel is None else base + [
                        'docker', 'run', '--rm', '--name', name + '-probe', 'harddind-cpu-touch:local', '/cpu-touch-probe']
                    call(sample, cmd, env)
                    records = [json.loads(line) for line in (b / (sample + '.stdout')).read_text().splitlines()]
                    assert not (b / (sample + '.stderr')).read_bytes()
                    assert [(x['bytes'], x['iteration']) for x in records] == [(s, i) for s in [1048576,4194304,16777216] for i in range(8)]
                    assert all(x['verified'] is True and x['page_size'] == 4096 and x['pages'] * x['page_size'] == x['bytes']
                               and all(type(x[f]) is int and x[f] >= 0 for f in fields) for x in records)
                    cell['samples'].append(records)
                    save()
            finally:
                if kernel:
                    st = harddind.read_state(instance)
                    if harddind.live(st) and st.get('phase') == 'ready':
                        call(label + '-inventory', base + ['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '-probe$'], env, 30)
                        if (b / (label + '-inventory.stdout')).read_text().strip():
                            call(label + '-remove', base + ['docker', 'rm', '-f', name + '-probe'], env, 30, False)
                    if harddind.live(harddind.read_state(instance)):
                        call(label + '-stop', base + ['stop'], env, required=False)
                    st = harddind.read_state(instance)
                    r['states'][name] = st
                    cell['normal_shutdown'] = st.get('phase') == 'stopped' and st.get('guest_exit_code') == 0 and not st.get('forced_shutdown', True)
                    save()
                    assert cell['normal_shutdown'], label + ' shutdown'
        cell['host_after'] = host_state()
        cell['completed'] = True
        save()
    r['summary'] = {label: {str(size): {field: statistics.median(x[field] for rows in cell['samples'] for x in rows if x['bytes'] == size)
            for field in fields} for size in [1048576, 4194304, 16777216]} for label, cell in r['cells'].items()}
    rc = 0
except BaseException:
    r['error'] = traceback.format_exc()
finally:
    try:
        for label, kernel, order in cells:
            if not kernel:
                continue
            name = 'fa-' + label
            instance = work / 'instances' / name
            st = harddind.read_state(instance)
            if harddind.live(st):
                call(label + '-final-stop', command(name) + ['stop'], dict(os.environ), required=False)
                st = harddind.read_state(instance)
            if st:
                r['states'][name] = st
        groups = {c['pid'] for c in r['commands'] if 'pid' in c}
        for st in r['states'].values():
            groups.update(st[k] for k in ['guest_pid','network_pid','supervisor_pid'] if st.get(k))
        r['groups'] = {str(g): group_members(g) for g in sorted(groups)}
        r['groups_empty'] = all(x == ([], []) for x in r['groups'].values())
        r['inputs_unchanged'] = bool(r.get('inputs')) and all(harddind.sha(Path(p)) == h for p,h in r['inputs'].items())
        r['sources_unchanged'] = bool(r.get('sources')) and all(harddind.sha(b / p) == h for p,h in r['sources'].items())
        if not (r['groups_empty'] and r['inputs_unchanged'] and r['sources_unchanged']):
            rc = 1
    except BaseException:
        r['cleanup_error'] = traceback.format_exc()
        rc = 1
    r.update(status='finished', exit_code=rc, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    save()
sys.exit(rc)
