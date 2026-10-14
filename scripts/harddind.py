#!/usr/bin/env python3
"""Experimental persistent stock Docker on UML; state lives under WORK/instances.

The ordinary Docker CLI talks to the real daemon. Published ports currently need
an explicit `forward` on the outer helper. Outer bind paths and GPU remain gaps.
"""
import argparse
import ctypes
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from guest_cleanup import cleanup_group


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def write_state(instance, state):
    tmp = instance / 'state.new'
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.replace(instance / 'state.json')


def start_time(pid):
    try:
        fields = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def live(state):
    return bool(state.get('supervisor_start') and start_time(state['supervisor_pid']) == state['supervisor_start'])


def read_state(instance):
    try:
        return json.loads((instance / 'state.json').read_text())
    except FileNotFoundError:
        return {}


def control(instance, command):
    path = instance / ('c-' + uuid.uuid4().hex[:8])
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        try:
            s.bind(str(path))
            s.connect(str(instance / 'network.ctl'))
            s.send(command.encode())
            result = json.loads(s.recv(32768))
            if isinstance(result, dict) and result.get('ok') is False:
                raise RuntimeError(result['error'])
            return result
        finally:
            path.unlink(missing_ok=True)


def api(port, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request('GET', path)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f'API {path}: HTTP {response.status}')
        return body
    finally:
        conn.close()


def mconsole(instance, path, command):
    client = instance / ('m-' + uuid.uuid4().hex[:8])
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        try:
            s.bind(str(client)); s.settimeout(3)
            data = command.encode()
            s.sendto(struct.pack('=III512s', 0xcafebabe, 2, len(data), data), str(path))
            reply = s.recv(524)
            err, _, length = struct.unpack('=III', reply[:12])
            if err:
                raise RuntimeError(reply[12:12 + length].decode(errors='replace'))
        finally:
            client.unlink(missing_ok=True)


def serve(a, work, instance):
    os.umask(0o077)
    lock = (instance / 'instance.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    build = (work / 'kernel-build.lock').open('a')
    fcntl.flock(build, fcntl.LOCK_SH)
    kernel = a.kernel.resolve()
    disk = a.disk.resolve()
    config = {'kernel': str(kernel), 'disk': str(disk), 'cpus': a.cpus, 'memory': a.memory, 'dns': a.dns, 'network_batch': a.network_batch, 'mtu': a.mtu,
              'kernel_sha256': sha(kernel), 'disk_sha256': sha(disk), 'network_sha256': sha(work / 'network-backend')}
    config_file = instance / 'config.json'
    if config_file.exists():
        previous = json.loads(config_file.read_text())
        previous.setdefault('dns', '10.0.2.3')
        previous.setdefault('network_batch', 1)
        previous.setdefault('mtu', 1500)
        if previous != config:
            raise RuntimeError('instance input changed; use a new instance name')
    config_file.write_text(json.dumps(config, indent=2) + '\n')
    run = uuid.uuid4().hex[:12]
    state = {'phase': 'starting', 'run': run, 'supervisor_pid': os.getpid(),
             'supervisor_start': start_time(os.getpid()), 'config': config,
             'runtime_sha256': sha(pathlib.Path(__file__).resolve())}
    write_state(instance, state)
    stopping = False
    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    helper = guest = None
    guest_end = helper_end = None
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getpid()
    def parent_death():
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
            os._exit(1)
    ctl = instance / 'network.ctl'
    ctl.unlink(missing_ok=True)  # The exclusive instance lock is held.
    try:
        guest_end, helper_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for s in (guest_end, helper_end):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        with (instance / f'network-{run}.log').open('x') as log:
            helper = subprocess.Popen([str(work / 'network-backend'), str(helper_end.fileno()), str(ctl), str(a.network_batch), str(a.mtu)],
                                      pass_fds=(helper_end.fileno(),), stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True, preexec_fn=parent_death)
        helper_end.close()
        until = time.monotonic() + 5
        while not ctl.exists():
            if helper.poll() is not None or time.monotonic() > until:
                raise RuntimeError('network backend did not start')
            time.sleep(0.05)
        for _ in range(10):
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
            try:
                control(instance, f'add tcp 127.0.0.1 {port} 2375')
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError('cannot allocate loopback API port')
        uml_dir = instance / 'uml'
        uml_dir.mkdir(exist_ok=True)
        tmp = instance / 'tmp'; tmp.mkdir(exist_ok=True)
        cmd = [str(kernel), f'umid={run}', f'uml_dir={uml_dir}/', f'mem={a.memory}', f'ncpus={a.cpus}',
               'seccomp=on', 'seccomp_threads=1', 'seccomp_broker_cache=1', 'seccomp_spin=256', 'defer_ipi=1',
               f'ubd0={instance / "root.cow"},{disk}', 'root=/dev/ubda', 'rw',
               'init=/sbin/harddind-runtime-init', 'con=null', 'con0=null,fd:1', 'ssl=null', 'panic=1',
               f'harddind_dns={a.dns}', f'harddind_mtu={a.mtu}',
               f'vec0:transport=fd,fd={guest_end.fileno()},gro=0,mtu={a.mtu},mac=02:00:00:00:02:15']
        with (instance / f'guest-{run}.log').open('x') as log:
            guest = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     pass_fds=(guest_end.fileno(),), env={**os.environ, 'TMPDIR': str(tmp)},
                                     start_new_session=True, preexec_fn=parent_death)
        guest_end.close()
        state.update({'guest_pid': guest.pid, 'network_pid': helper.pid, 'api_port': port,
                      'mconsole': str(uml_dir / run / 'mconsole'), 'command': cmd})
        write_state(instance, state)
        deadline = time.monotonic() + 120
        while not stopping:
            if guest.poll() is not None or helper.poll() is not None:
                raise RuntimeError('guest or network backend exited unexpectedly')
            if state['phase'] == 'starting':
                try:
                    if api(port, '/_ping') == b'OK':
                        if a.mtu != 1500:
                            console = (instance / f'guest-{run}.log').read_text(errors='replace')
                            if f'HARDDIND_RUNTIME_MTU={a.mtu}' not in console:
                                state['fixture_error'] = 'guest fixture does not confirm requested MTU'
                                break
                        state['phase'] = 'ready'; state['ready_at'] = time.time(); write_state(instance, state)
                except (OSError, http.client.HTTPException, RuntimeError):
                    pass
                if state['phase'] != 'ready' and time.monotonic() > deadline:
                    raise RuntimeError('Docker API readiness timed out')
            time.sleep(0.1)
        if state.get('fixture_error'): raise RuntimeError(state['fixture_error'])
    except BaseException as error:
        state['error'] = f'{type(error).__name__}: {error}'
    finally:
        state['phase'] = 'stopping'; write_state(instance, state)
        if guest is not None:
            forced = False
            if guest.poll() is None:
                try:
                    mconsole(instance, pathlib.Path(state['mconsole']), 'cad')
                    guest.wait(timeout=45)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    state['shutdown_error'] = str(error)
                    forced = True
                    os.killpg(guest.pid, signal.SIGKILL)
                    guest.wait()
            state['guest_exit_code'] = guest.returncode
            state['guest_cleanup'] = cleanup_group(guest.pid)
            state['forced_shutdown'] = forced
        if helper is not None:
            if helper.poll() is None:
                helper.terminate()
                try: helper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(helper.pid, signal.SIGKILL); helper.wait()
            state['network_exit_code'] = helper.returncode
            state['network_cleanup'] = cleanup_group(helper.pid)
        for s in (guest_end, helper_end):
            if s is not None: s.close()
        ctl.unlink(missing_ok=True)
        clean = (guest is not None and state.get('guest_exit_code') == 0 and
                 state.get('guest_cleanup', {}).get('clean_shutdown') and
                 state.get('network_exit_code') == 0 and state.get('network_cleanup', {}).get('clean_shutdown'))
        state['phase'] = 'stopped' if clean and not state.get('error') and not state.get('forced_shutdown') else 'failed'
        state['stopped_at'] = time.time()
        write_state(instance, state)
        (instance / f'result-{run}.json').write_text(json.dumps(state, indent=2) + '\n')
    return 0 if state['phase'] == 'stopped' else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('name')
    subs = p.add_subparsers(dest='action', required=True)
    for name in ('start', 'serve'):
        s = subs.add_parser(name)
        s.add_argument('--kernel', type=pathlib.Path, required=True)
        s.add_argument('--disk', type=pathlib.Path)
        s.add_argument('--cpus', type=int, default=4)
        s.add_argument('--memory', default='4G')
        s.add_argument('--dns', default='10.0.2.3', help='IPv4 DNS resolver; default forwards the outer resolver')
        s.add_argument('--network-batch', type=int, choices=range(1, 65), default=1, metavar='1..64', help='experimental link batch size (default 1)')
        s.add_argument('--mtu', type=int, default=1500, help='IPv4 link and bridge MTU, 576..65520 (default 1500)')
    for name in ('status', 'stop'):
        subs.add_parser(name)
    subs.add_parser('docker').add_argument('args', nargs=argparse.REMAINDER)
    subs.add_parser('forward').add_argument('args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if not a.name or len(a.name) > 32 or not all(c.isalnum() or c in '-_' for c in a.name):
        p.error('invalid instance name')
    work = a.work.resolve(); instance = work / 'instances' / a.name
    if len(str(instance / 'network.ctl').encode()) > 100:
        p.error('work path too long for Unix sockets')
    instance.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = read_state(instance)
    if a.action in ('start', 'serve'):
        if not 1 <= a.cpus <= 64 or not a.memory[:-1].isdigit() or a.memory[-1:] not in ('M', 'G'):
            p.error('invalid resources')
        if not 576 <= a.mtu <= 65520: p.error('mtu must be between 576 and 65520')
        try: a.dns = str(ipaddress.IPv4Address(a.dns))
        except ipaddress.AddressValueError: p.error('dns must be an IPv4 resolver address')
        a.disk = a.disk or work / 'runtime.ext4'
        if a.action == 'serve': return serve(a, work, instance)
        if live(state):
            if state['phase'] != 'ready': raise RuntimeError('instance is still starting or stopping')
            requested = {'kernel': str(a.kernel.resolve()), 'disk': str(a.disk.resolve()), 'cpus': a.cpus, 'memory': a.memory, 'dns': a.dns, 'network_batch': a.network_batch, 'mtu': a.mtu}
            if any(state['config'].get(key, {'dns': '10.0.2.3', 'network_batch': 1, 'mtu': 1500}.get(key)) != value for key, value in requested.items()):
                raise RuntimeError('running instance uses different inputs; stop it or use another name')
            print(json.dumps(state, indent=2)); return 0
        cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), str(work), a.name, 'serve',
               '--kernel', str(a.kernel.resolve()), '--disk', str(a.disk.resolve()),
               '--cpus', str(a.cpus), '--memory', a.memory, '--dns', a.dns, '--network-batch', str(a.network_batch), '--mtu', str(a.mtu)]
        with (instance / 'supervisor.log').open('a') as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        until = time.monotonic() + 150
        while time.monotonic() < until:
            state = read_state(instance)
            if state.get('supervisor_pid') == proc.pid and state.get('phase') == 'ready':
                print(json.dumps(state, indent=2)); return 0
            if proc.poll() is not None:
                raise RuntimeError('startup failed; see instance logs')
            time.sleep(0.2)
        if start_time(proc.pid) is not None: proc.terminate()
        raise RuntimeError('startup timed out')
    if a.action == 'status':
        print(json.dumps({**state, 'supervisor_alive': live(state)}, indent=2)); return 0
    if a.action == 'stop':
        if live(state):
            os.kill(state['supervisor_pid'], signal.SIGTERM)
            until = time.monotonic() + 60
            while live(state) and time.monotonic() < until: time.sleep(0.2)
            if live(state): raise RuntimeError('supervisor did not finish shutdown')
        result = read_state(instance); print(json.dumps(result, indent=2))
        return 0 if result.get('phase') == 'stopped' else 1
    if not live(state) or state.get('phase') != 'ready': raise RuntimeError('instance is not ready')
    if a.action == 'forward':
        print(json.dumps(control(instance, ' '.join(a.args)), indent=2)); return 0
    if a.args[:1] == ['--']: a.args = a.args[1:]
    client = work / 'client'; config = instance / 'docker-config'; config.mkdir(exist_ok=True, mode=0o700)
    config_file = config / 'config.json'
    if not config_file.exists(): config_file.write_text(json.dumps({'cliPluginsExtraDirs': [str(client / 'cli-plugins')]}))
    env = {**os.environ, 'DOCKER_HOST': f'tcp://127.0.0.1:{state["api_port"]}', 'DOCKER_CONFIG': str(config)}
    for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'): env.pop(key, None)
    os.execve(str(client / 'ld-musl-x86_64.so.1'), [str(client / 'ld-musl-x86_64.so.1'), str(client / 'docker'), *a.args], env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error))
