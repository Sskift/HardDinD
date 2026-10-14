from pathlib import Path
import datetime, fcntl, hashlib, io, json, os, shlex, shutil, signal
import subprocess, sys, tarfile, time, traceback

b = Path(__file__).resolve().parent
w = b.parent
work = b / 'run'
repo = b / 'repo'
sys.path.insert(0, str(repo / 'scripts'))
import harddind
from guest_cleanup import cleanup_group

name = 'cpu-observer-v1'
instance = work / 'instances' / name
base = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), name]
binary = b / 'cpu-limit-probe'
kernel = w / 'cpu-cpuset-kernel-v1'
disk = w / 'runtime-shares-v1.ext4'
inputs = [Path(__file__).resolve(), binary, kernel, disk,
          repo / 'src/cpu_capture_probe.c', repo / 'scripts/harddind.py',
          repo / 'scripts/guest_cleanup.py', repo / 'scripts/port_driver.py',
          repo / 'scripts/result_checks.py', (work / 'network-backend').resolve(),
          work / 'client/docker']
report = {'status': 'running', 'pid': os.getpid(), 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'kind': 'cpu_observer_placement_diagnostic', 'full_acceptance': False,
          'performance_acceptance': False, 'commands': [], 'cells': {},
          'sha256': {str(p): harddind.sha(p) for p in inputs},
          'limitations': ['One sequential run; no causal attribution to host scheduling',
                         'Both Docker exec and the observer consume cgroup quota',
                         'No performance threshold; quiet cells do not exclude intermittent stalls']}

def host_state():
    return {p: Path(p).read_text() for p in ['/proc/loadavg', '/sys/fs/cgroup/cpu.stat',
            '/sys/fs/cgroup/cpu.max', '/sys/fs/cgroup/cpu.pressure']}

def save():
    (b / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

def run(label, cmd, stdin=None, timeout=90, required=True):
    start = time.monotonic_ns()
    with (b / (label + '.stdout')).open('xb') as out, (b / (label + '.stderr')).open('xb') as err:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True)
        timed_out = False
        try:
            p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate(timeout=15)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=15)
            cleanup = cleanup_group(p.pid)
    row = {'label': label, 'command': cmd, 'pid': p.pid, 'exit_code': p.returncode,
           'timed_out': timed_out, 'begin_ns': start, 'end_ns': time.monotonic_ns(), 'cleanup': cleanup}
    report['commands'].append(row)
    save()
    if required and (p.returncode or timed_out or not cleanup['clean_shutdown']):
        raise RuntimeError('command failed: ' + label)
    return (b / (label + '.stdout')).read_text()

def cli(label, *args, **kw):
    return run(label, base + ['docker', *args], **kw)

capture = ['/cpu-limit-probe', 'capture-test', '--max', '/sys/fs/cgroup/cpu.max',
           '--stat', '/sys/fs/cgroup/cpu.stat', '--proc', '/proc/stat',
           '--count', '50', '--interval-ms', '100']

def sample_cell(label, flags):
    report['cells'][label] = {'host_before': host_state(), 'flags': flags}
    text = cli(label, 'run', '--rm', '--name', label, *flags, 'harddind-observer:local', *capture)
    rows = [json.loads(line) for line in text.splitlines()]
    if len(rows) != 50:
        raise RuntimeError('incomplete capture count: ' + label)
    report['cells'][label].update(samples=rows, host_after=host_state())
    save()

save()
rc = 1
try:
    with (work / 'kernel-build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        report['host_before'] = host_state()
        run('start', base + ['start', '--kernel', str(kernel), '--disk', str(disk), '--cpus', '4'], timeout=180)
        report['ready_state'] = harddind.read_state(instance)
        cli('info', 'info', '--format', '{{.NCPU}} {{.ServerVersion}} {{.KernelVersion}}')
        bundle = io.BytesIO()
        with tarfile.open(fileobj=bundle, mode='w') as archive:
            for directory in ['dev', 'proc', 'sys', 'tmp']:
                info = tarfile.TarInfo(directory); info.type = tarfile.DIRTYPE; info.mode = 0o755
                archive.addfile(info)
            info = archive.gettarinfo(str(binary), arcname='cpu-limit-probe'); info.mode = 0o755
            with binary.open('rb') as src: archive.addfile(info, src)
        cli('import', 'import', '-', 'harddind-observer:local', stdin=bundle.getvalue(), timeout=120)
        sample_cell('quiet-quota', ['--cpus', '0.5'])
        sample_cell('quiet-unlimited', [])
        cid = cli('busy-start', 'run', '-d', '--name', 'observer-busy', '--cpus', '0.5',
                  'harddind-observer:local', '/cpu-limit-probe', 'spin', '--threads', '4',
                  '--duration-ms', '40000', '--sample-ms', '100').strip()
        report['busy_id'] = cid
        cli('busy-inspect-before', 'inspect', 'observer-busy')
        sample_cell('separate-observer', [])
        text = cli('same-observer', 'exec', 'observer-busy', *capture)
        rows = [json.loads(line) for line in text.splitlines()]
        if len(rows) != 50: raise RuntimeError('incomplete same-cgroup capture')
        report['cells']['same-observer'] = {'samples': rows, 'shares_busy_cgroup': True}
        cli('busy-inspect-after', 'inspect', 'observer-busy')
        exit_text = cli('busy-wait', 'wait', 'observer-busy', timeout=65).strip()
        if exit_text != '0': raise RuntimeError('busy probe failed: ' + exit_text)
        cli('busy-logs', 'logs', 'observer-busy')
        cli('busy-final', 'inspect', 'observer-busy')
        cli('busy-remove', 'rm', 'observer-busy')
        report['remaining_containers'] = cli('inventory', 'ps', '-aq').strip()
        if report['remaining_containers']: raise RuntimeError('remaining containers')
        run('stop', base + ['stop'], timeout=90)
        rc = 0
except BaseException:
    report['error'] = traceback.format_exc()
finally:
    try:
        state = harddind.read_state(instance)
        if harddind.live(state) and state.get('phase') == 'ready':
            for n in ['quiet-quota', 'quiet-unlimited', 'separate-observer', 'observer-busy']:
                cli('cleanup-' + n, 'rm', '-f', n, timeout=20, required=False)
        if harddind.live(harddind.read_state(instance)):
            run('final-stop', base + ['stop'], timeout=90, required=False)
        state = harddind.read_state(instance)
        report['final_state'] = state
        report['normal_shutdown'] = (state.get('phase') == 'stopped' and not state.get('forced_shutdown')
            and state.get('guest_cleanup', {}).get('clean_shutdown')
            and state.get('network_cleanup', {}).get('clean_shutdown'))
        report['inputs_unchanged'] = all(harddind.sha(Path(p)) == h for p, h in report['sha256'].items())
        report['host_after'] = host_state()
        if not report['normal_shutdown'] or not report['inputs_unchanged']: rc = 1
    except BaseException:
        report['cleanup_error'] = traceback.format_exc(); rc = 1
    report.update(status='finished', exit_code=rc, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    save()
sys.exit(rc)
