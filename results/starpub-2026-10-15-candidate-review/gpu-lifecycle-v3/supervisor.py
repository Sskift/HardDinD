from pathlib import Path
import datetime, fcntl, hashlib, importlib.util, json, os, signal
import subprocess, sys, traceback

b = Path(__file__).resolve().parent
w = b.parent
sys.path.insert(0, str(b / 'scripts'))
from guest_cleanup import cleanup_group

report = {'status': 'running', 'stage': 'waiting_for_build_lock', 'pid': os.getpid(),
          'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'full_acceptance': False, 'internal_gpu_acceptance': 'not_tested',
          'performance_acceptance': False, 'commands': [],
          'source_sha256': {str(p.relative_to(b)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in b.rglob('*') if p.is_file()}}

def save():
    (b / 'owner-report.json').write_text(json.dumps(report, indent=2) + '\n')

def call(label, cmd, *, timeout=60, env=None, cwd=None):
    with (b / (label + '.stdout')).open('xb') as out, (b / (label + '.stderr')).open('xb') as err:
        p = subprocess.Popen(cmd, cwd=cwd or b, env=env, stdin=subprocess.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True)
        timed_out = False
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            code = p.wait(timeout=15)
        finally:
            cleanup = cleanup_group(p.pid)
    row = {'label': label, 'command': cmd, 'pid': p.pid, 'exit_code': code,
           'timed_out': timed_out, 'cleanup': cleanup}
    report['commands'].append(row)
    save()
    if code or timed_out or not cleanup['clean_shutdown']:
        raise RuntimeError('failed command: ' + label)
    return (b / (label + '.stdout')).read_text()

save()
rc = 1
try:
    with (w / 'kernel-build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report['stage'] = 'validation'; save()
        os.sched_setaffinity(0, {0, 1, 2, 3})
        flags = ['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=gnu11', '-pthread']
        call('build-original-protocol', flags + ['original_protocol_regression.c', '-lm', '-ldl', '-o', 'original-protocol'])
        call('original-protocol', [str(b / 'original-protocol')])
        call('build-fixed-protocol', flags + ['tests/owner_protocol_regression.c', '-lm', '-ldl', '-o', 'fixed-protocol'])
        call('fixed-protocol', [str(b / 'fixed-protocol'), '--expect-fixed'])
        call('build-probe', flags + ['src/cuda_lifecycle_probe.c', '-lm', '-ldl', '-o', 'cuda-lifecycle-probe'])
        call('build-sanitizer-fixture', flags + ['-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
             'tests/cuda_lifecycle_host_fixture.c', '-lm', '-ldl', '-o', 'host-fixture'])
        call('sanitizer-fixture', [str(b / 'host-fixture')])
        call('checker-tests', ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_gpu_lifecycle_checks.py', '-v'])
        report['binary_sha256'] = hashlib.sha256((b / 'cuda-lifecycle-probe').read_bytes()).hexdigest()
        report['driver_version'] = Path('/proc/driver/nvidia/version').read_text()
        report['library_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path('/lib/x86_64-linux-gnu/libcuda.so.1'),
                      Path('/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1')]}
        query = ['nvidia-smi', '--query-gpu=uuid,name,memory.total,driver_version', '--format=csv,noheader,nounits']
        before = call('inventory-before', query)
        uuids = [line.split(',')[0].strip() for line in before.splitlines() if line.strip()]
        if len(uuids) != 4 or len(set(uuids)) != 4:
            raise RuntimeError('expected four distinct native GPU UUIDs')
        report['stage'] = 'native_gpu_lifecycle'; save()
        content = call('native', [str(b / 'cuda-lifecycle-probe')], timeout=120,
                       env={**os.environ, 'CUDA_CACHE_PATH': str(b / 'native-cache')})
        after = call('inventory-after', query)
        if after != before: raise RuntimeError('native inventory changed')
        spec = importlib.util.spec_from_file_location('checks', b / 'scripts/gpu_lifecycle_checks.py')
        checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(checks)
        verdict = checks.analyze(content, {'uuids': uuids})
        report['native_verdict'] = verdict
        (b / 'native-verdict.json').write_text(json.dumps(verdict, indent=2) + '\n')
        if not verdict['ok']: raise RuntimeError('native lifecycle checker failed')
        report['inputs_unchanged'] = all(hashlib.sha256((b / n).read_bytes()).hexdigest() == h
                                         for n, h in report['source_sha256'].items())
        report['libraries_unchanged'] = all(hashlib.sha256(Path(n).read_bytes()).hexdigest() == h
                                            for n, h in report['library_sha256'].items())
        if not report['inputs_unchanged'] or not report['libraries_unchanged']:
            raise RuntimeError('runtime inputs changed')
        rc = 0
except BaseException:
    report['error'] = traceback.format_exc()
report.update(status='finished', exit_code=rc, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
save()
sys.exit(rc)
