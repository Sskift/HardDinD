#!/usr/bin/env python3
"""Exercise real stock Docker through the persistent unprivileged runtime.

Networking and lifecycle evidence only; not full Docker or performance acceptance.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shlex
import socket
import statistics
import subprocess
import sys
import time
import uuid
import harddind

p = argparse.ArgumentParser()
p.add_argument('work', type=pathlib.Path)
p.add_argument('--kernel', type=pathlib.Path, required=True)
p.add_argument('--name', required=True)
a = p.parse_args()
if not a.name or len(a.name) > 32 or not all(c.isalnum() or c in '-_' for c in a.name): p.error('invalid name')
work = a.work.resolve(); repo = pathlib.Path(__file__).resolve().parents[1]
out = work / 'results'; out.mkdir(exist_ok=True)
report_path = out / (a.name + '.json'); log_path = out / (a.name + '.log')
instance = work / 'instances' / a.name
if instance.exists() or report_path.exists() or log_path.exists(): p.error('refusing existing instance or evidence')
os.sched_setaffinity(0, [0, 1, 2, 3])
lock = json.loads((repo / 'sources.lock.json').read_text())
image = lock['network_test_image']['reference']
base = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), a.name]
checks = {name: 'not_tested' for name in ['daemon_api', 'cold_image_pull', 'container_https', 'network_build',
    'exec_stdin_tty', 'explicit_tcp_publish', 'explicit_udp_publish', 'forward_removal',
    'tcp_echo_comparison', 'persistent_restart', 'clean_shutdown']}
report = {'kind': 'runtime_network_subset', 'checks': checks, 'full_acceptance': False,
          'image': image, 'host_cpu_affinity': sorted(os.sched_getaffinity(0)), 'timings': {},
          'sha256': {str(p.relative_to(work)): harddind.sha(p) for p in [work / 'network-probe', work / 'network-backend']},
          'input_kernel_sha256': harddind.sha(a.kernel),
          'sources': {str(path.relative_to(repo)): harddind.sha(path) for path in [repo / 'scripts/run-runtime.py',
              repo / 'scripts/harddind.py', repo / 'src/network_backend.c', repo / 'src/network_probe.c', repo / 'sources.lock.json']}}
def host_state():
    return {name: pathlib.Path(path).read_text() for name, path in {
        'cpu_stat': '/sys/fs/cgroup/cpu.stat', 'cpu_pressure': '/sys/fs/cgroup/cpu.pressure', 'loadavg': '/proc/loadavg'}.items()}
report['host_before'] = host_state()
log = log_path.open('x')
active = None
native = None
def run(args, input=None, timeout=120):
    log.write('$ ' + shlex.join(args) + '\n'); log.flush()
    started = time.monotonic()
    r = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    text = r.stdout.decode(errors='replace')
    log.write(text + f'\n[exit={r.returncode} seconds={time.monotonic()-started:.6f}]\n'); log.flush()
    if r.returncode: raise RuntimeError(f'command exited {r.returncode}: {shlex.join(args)}')
    return text
def cli(*args, **kwargs): return run(base + ['docker', *args], **kwargs)
def begin(name):
    global active
    active = name; checks[name] = 'running'; print('starting ' + name, flush=True)
def passed():
    checks[active] = 'pass'; print(active + ' passed', flush=True)
def port(udp=False):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]
def wait_tcp(p):
    until = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection(('127.0.0.1', p), timeout=1): return
        except OSError:
            if time.monotonic() >= until: raise
            time.sleep(0.1)
def echo(p, size=8*1024*1024):
    payload = bytes(range(256)) * 256
    started = time.monotonic()
    with socket.create_connection(('127.0.0.1', p), timeout=5) as s:
        s.settimeout(15)
        for _ in range(size // len(payload)):
            s.sendall(payload)
            data = bytearray()
            while len(data) < len(payload):
                chunk = s.recv(len(payload)-len(data))
                if not chunk: raise RuntimeError('early TCP EOF')
                data.extend(chunk)
            if data != payload: raise RuntimeError('TCP data corruption')
    seconds = time.monotonic() - started
    return {'payload_bytes_each_direction': size, 'seconds': seconds, 'payload_mbps_each_direction': size*8/seconds/1000000}
try:
    begin('daemon_api')
    run(base + ['start', '--kernel', str(a.kernel.resolve())], timeout=180)
    version = json.loads(cli('version', '--format', '{{json .Server}}'))
    report['server'] = version
    assert version['Version'] == '29.8.2'
    assert int(cli('info', '--format', '{{.NCPU}}').strip()) == 4
    assert not json.loads(cli('image', 'ls', '--format', 'json').strip() or '[]')
    passed()
    begin('cold_image_pull')
    t = time.monotonic(); cli('pull', image, timeout=180); report['timings']['cold_pull_seconds'] = time.monotonic()-t
    assert image in json.loads(cli('inspect', '--format', '{{json .RepoDigests}}', image))
    assert cli('run', '--rm', image, 'cat', '/etc/alpine-release').strip() == '3.24.2'
    passed()
    begin('container_https')
    download = 'wget -q -T 30 -O /tmp/alpine.tar.gz ' + shlex.quote(lock['alpine']['url']) + ' && echo ' + shlex.quote(lock['alpine']['sha256']+'  /tmp/alpine.tar.gz') + ' | sha256sum -c -'
    cli('run', '--rm', image, 'sh', '-ec', download)
    passed()
    begin('network_build')
    dockerfile = f'FROM {image}\nRUN {download} && echo harddind-build-ok > /built\nCMD ["cat", "/built"]\n'
    (out / (a.name + '.Dockerfile')).write_text(dockerfile)
    cli('build', '--no-cache', '--progress=plain', '-t', 'harddind-network-test', '-', input=dockerfile.encode(), timeout=180)
    assert cli('run', '--rm', 'harddind-network-test').strip() == 'harddind-build-ok'
    passed()
    begin('exec_stdin_tty')
    cli('run', '-d', '--name', 'persist', '--restart=always', '-v', 'persistent-state:/data', image, 'sleep', '86400')
    token = uuid.uuid4().hex
    assert cli('exec', '-i', 'persist', 'sh', '-c', 'cat > /data/marker; cat /data/marker', input=token.encode()).strip() == token
    assert 'tty-ok' in cli('exec', '-t', 'persist', 'sh', '-c', 'test -t 1 && echo tty-ok')
    passed()
    tcp, udp = port(), port(True)
    begin('explicit_tcp_publish')
    cli('create', '--name', 'tcp-echo', '-p', f'{tcp}:7777/tcp', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
    cli('cp', str(work/'network-probe'), 'tcp-echo:/netprobe'); cli('start', 'tcp-echo')
    harddind.control(instance, f'add tcp 127.0.0.1 {tcp} {tcp}')
    wait_tcp(tcp); echo(tcp, 65536)
    passed()
    begin('explicit_udp_publish')
    cli('create', '--name', 'udp-echo', '-p', f'{udp}:7777/udp', image, '/netprobe', 'udp', '7777', '0.0.0.0')
    cli('cp', str(work/'network-probe'), 'udp-echo:/netprobe'); cli('start', 'udp-echo')
    harddind.control(instance, f'add udp 127.0.0.1 {udp} {udp}')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(5)
        for i in range(20):
            data = bytes([i])*1200; s.sendto(data, ('127.0.0.1', udp)); reply, _ = s.recvfrom(2048)
            assert reply == data
    passed()
    begin('tcp_echo_comparison')
    native_port = port()
    native = subprocess.Popen([str(work/'network-probe'), 'tcp', str(native_port)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    wait_tcp(native_port)
    samples = {'native': [], 'nested_docker': []}
    for mode in ['native', 'nested_docker', 'nested_docker', 'native']:
        for _ in range(3): samples[mode].append(echo(native_port if mode == 'native' else tcp))
    report['tcp_echo'] = {'samples': samples, 'order': ['native', 'nested_docker', 'nested_docker', 'native'],
                         'median_payload_mbps_each_direction': {m:statistics.median(r['payload_mbps_each_direction'] for r in rows) for m,rows in samples.items()},
                         'full_performance_acceptance': False, 'cpu_exclusive': False}
    passed()
    begin('forward_removal')
    harddind.control(instance, f'del tcp 127.0.0.1 {tcp} {tcp}')
    try:
        with socket.create_connection(('127.0.0.1', tcp), timeout=2): pass
    except OSError: pass
    else: raise RuntimeError('removed TCP forward is still listening')
    harddind.control(instance, f'del udp 127.0.0.1 {udp} {udp}')
    assert len(harddind.control(instance, 'list')) == 1
    passed()
    begin('persistent_restart')
    before = json.loads(cli('inspect', '--format', '{{json .Id}}', image))
    report['network_stats'] = harddind.control(instance, 'stats')
    run(base + ['stop'], timeout=70)
    first = harddind.read_state(instance)
    assert first['phase'] == 'stopped' and first['guest_cleanup']['clean_shutdown'] and not first['forced_shutdown']
    report['first_run'] = first
    run(base + ['start', '--kernel', str(a.kernel.resolve())], timeout=180)
    assert json.loads(cli('inspect', '--format', '{{json .Id}}', image)) == before
    until = time.monotonic() + 30
    while cli('inspect', '--format', '{{.State.Running}}', 'persist').strip() != 'true':
        if time.monotonic() > until: raise RuntimeError('restart policy did not restore container')
        time.sleep(0.2)
    assert cli('exec', 'persist', 'cat', '/data/marker').strip() == token
    passed()
except BaseException as error:
    if active: checks[active] = 'fail'
    report['error'] = f'{type(error).__name__}: {error}'
    print(report['error'], flush=True)
finally:
    if native is not None:
        native.terminate(); native.wait(timeout=5)
        report['native_cleanup'] = harddind.cleanup_group(native.pid)
    try:
        run(base + ['stop'], timeout=70)
        state = harddind.read_state(instance)
        report['final_run'] = state
        clean = state.get('phase') == 'stopped' and state.get('guest_cleanup',{}).get('clean_shutdown') and not state.get('forced_shutdown')
        checks['clean_shutdown'] = 'pass' if clean else 'fail'
    except BaseException as error:
        checks['clean_shutdown'] = 'fail'; report['cleanup_error'] = str(error)
    report['host_after'] = host_state()
    report['subset_passed'] = all(value == 'pass' for value in checks.values())
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    log.close()
print(json.dumps({'report': str(report_path), 'checks': checks, 'full_acceptance': False}, indent=2))
raise SystemExit(0 if report['subset_passed'] else 1)
