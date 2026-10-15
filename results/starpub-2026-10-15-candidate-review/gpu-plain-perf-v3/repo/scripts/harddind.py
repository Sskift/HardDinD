#!/usr/bin/env python3
"""Experimental persistent stock Docker on UML; state lives under WORK/instances.

The ordinary Docker CLI talks to the real daemon. The optional published-port
driver requires the patched Moby fixture. Outer bind paths and GPU remain gaps.
"""
import argparse
import base64
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
from guest_cleanup import cleanup_group, group_members
from port_driver import PortDriver
from result_checks import memfd_backing_evidence


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def share_specs(paths):
    result = []
    protected = ('/', '/proc', '/sys', '/dev', '/run', '/bin', '/sbin', '/lib', '/usr', '/var/lib/docker', '/var/lib/containerd')
    for path in paths:
        target = pathlib.Path(os.path.abspath(path))
        if '\n' in str(target) or '\r' in str(target):
            raise ValueError('shared path cannot contain a newline')
        if any(target == pathlib.Path(p) or pathlib.Path(p).is_relative_to(target) for p in protected):
            raise ValueError('share would cover a required guest system path')
        source = target.resolve(strict=True)
        if not source.is_dir(): raise ValueError('shared source must be a directory')
        if any(s['target'] == str(target) for s in result): raise ValueError('duplicate shared path')
        result.append({'source': str(source), 'target': str(target), 'tag': 'harddind'+str(len(result))})
    if len(result) > 8: raise ValueError('at most 8 shared directories per experimental instance')
    return result


def share_argument(share):
    return share['tag'] + ':' + base64.b64encode(os.fsencode(share['target'])).decode()


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
    # The filesystem path may exceed sockaddr_un.sun_path even though UML
    # bound it relative to its instance cwd. Resolve the same socket through
    # an open directory, without changing this multithreaded process's cwd.
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            s.bind(str(client)); s.settimeout(3)
            data = command.encode()
            address = f'/proc/self/fd/{directory}/{path.name}'
            s.sendto(struct.pack('=III512s', 0xcafebabe, 2, len(data), data), address)
            reply = s.recv(524)
            err, _, length = struct.unpack('=III', reply[:12])
            if err:
                raise RuntimeError(reply[12:12 + length].decode(errors='replace'))
        finally:
            os.close(directory)
            client.unlink(missing_ok=True)


def shutdown_diagnostics(instance, state):
    """Read only this guest's process group and request guest stack dumps."""
    report = {'captured_at': time.time(), 'process_group': state['guest_pid'], 'processes': [], 'sysrq': {}}
    live_pids, zombies = group_members(state['guest_pid'])
    for pid in live_pids + zombies:
        path = pathlib.Path('/proc') / str(pid)
        row = {'pid': pid}
        for name in ('stat', 'comm', 'wchan', 'syscall'):
            try: row[name] = (path / name).read_text().strip()
            except OSError as error: row[name + '_error'] = str(error)
        report['processes'].append(row)
    for key in ('w', 't', 'l'):
        try:
            mconsole(instance, pathlib.Path(state['mconsole']), 'sysrq ' + key)
            report['sysrq'][key] = 'requested; output is in the guest console log'
        except (OSError, RuntimeError) as error:
            report['sysrq'][key] = str(error)
    path = instance / ('shutdown-diagnostic-' + state['run'] + '.json')
    path.write_text(json.dumps(report, indent=2) + '\n')
    return str(path)


def serve(a, work, instance):
    os.umask(0o077)
    lock = (instance / 'instance.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    build = (work / 'kernel-build.lock').open('a')
    fcntl.flock(build, fcntl.LOCK_SH)
    kernel = a.kernel.resolve()
    disk = a.disk.resolve()
    config = {'kernel': str(kernel), 'disk': str(disk), 'cpus': a.cpus, 'memory': a.memory, 'dns': a.dns, 'network_batch': a.network_batch, 'mtu': a.mtu,
              'kernel_sha256': sha(kernel), 'disk_sha256': sha(disk), 'network_sha256': sha(work / 'network-backend'),
              'publish_ports': a.publish_ports, 'gpu_control_experiment': a.gpu_control_experiment, 'gpu_async_wait': a.gpu_async_wait,
              'memfd_ram': a.memfd_ram, 'memfd_stub': a.memfd_stub,
              'shares': share_specs(a.share)}
    if config['shares']:
        config['virtiofsd'] = str(a.virtiofsd)
        config['virtiofsd_sha256'] = sha(a.virtiofsd)
        config['share_inline_flock'] = a.share_inline_flock
        config['share_posix_locks'] = a.share_posix_locks
        config['share_cache'] = a.share_cache
        config['share_profile'] = a.share_profile
    if a.publish_ports:
        config['port_driver_sha256'] = sha(pathlib.Path(__file__).with_name('port_driver.py'))
    config_file = instance / 'config.json'
    if config_file.exists():
        previous = json.loads(config_file.read_text())
        previous.setdefault('dns', '10.0.2.3')
        previous.setdefault('gpu_control_experiment', False)
        previous.setdefault('gpu_async_wait', False)
        previous.setdefault('memfd_ram', False)
        previous.setdefault('memfd_stub', False)
        if previous.get('shares'):
            previous.setdefault('share_profile', False)
            previous.setdefault('share_inline_flock', False)
            previous.setdefault('share_posix_locks', False)
            previous.setdefault('virtiofsd', str((work / 'virtiofsd').resolve()))
        previous.setdefault('network_batch', 1)
        previous.setdefault('mtu', 1500)
        previous.setdefault('publish_ports', False)
        previous.setdefault('shares', [])
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
    port_parent = port_guest = relay = None
    filesystems = []
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getpid()
    def parent_death():
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
            os._exit(1)
    ctl = instance / 'network.ctl'
    ctl.unlink(missing_ok=True)  # The exclusive instance lock is held.
    try:
        for share in config['shares']:
            path = instance / (share['tag']+'.sock')
            path.unlink(missing_ok=True)
            cmd = [str(a.virtiofsd), '--socket-path', str(path), '--shared-dir', share['source'],
                   '--tag', share['tag'], '--sandbox', 'chroot', '--cache', a.share_cache,
                   '--inode-file-handles=never', '--xattr', '--posix-acl', '--announce-submounts', '--shared-flock',
                   '--thread-pool-size', '4']
            if a.share_profile: cmd.append('--profile-requests')
            if a.share_inline_flock: cmd.append('--inline-flock')
            if a.share_posix_locks: cmd.append('--shared-posix-locks')
            with (instance/f"{share['tag']}-{run}.log").open('x') as log:
                fs = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True, preexec_fn=parent_death)
            filesystems.append((fs, path, share, cmd))
            until = time.monotonic()+5
            while not path.exists():
                if fs.poll() is not None or time.monotonic() > until:
                    raise RuntimeError('virtiofsd did not start: '+share['target'])
                time.sleep(.05)
        state['filesystems'] = [{'pid': fs.pid, 'share': share, 'command': cmd} for fs, _, share, cmd in filesystems]
        write_state(instance, state)
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
        cmd = [str(kernel), f'umid={run}', 'uml_dir=uml/', f'mem={a.memory}', f'ncpus={a.cpus}',
               'seccomp=on', 'seccomp_threads=1', 'seccomp_broker_cache=1', 'seccomp_spin=256', 'defer_ipi=1',
               f'ubd0={instance / "root.cow"},{disk}', 'root=/dev/ubda', 'rw',
               'init=/sbin/harddind-runtime-init', 'con=null', 'con0=null,fd:1', 'ssl=null', 'panic=1',
               f'harddind_dns={a.dns}', f'harddind_mtu={a.mtu}',
               f'vec0:transport=fd,fd={guest_end.fileno()},gro=0,mtu={a.mtu},mac=02:00:00:00:02:15']
        if a.gpu_control_experiment:
            cmd.append('gpu_bridge=1')
        if a.gpu_async_wait: cmd.append('gpu_async_wait=1')
        if a.memfd_ram: cmd.append('memfd_ram=1')
        if a.memfd_stub: cmd.append('memfd_stub=1')
        for _, path, share, _ in filesystems:
            cmd += [f'virtio_uml.device={path}:26', 'harddind_share='+share_argument(share)]
        inherited = [guest_end.fileno()]
        if a.publish_ports:
            port_parent, port_guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            inherited.append(port_guest.fileno())
            cmd += [f'ssl0=fd:{port_guest.fileno()}', 'harddind_ports=1']
        if sum(len(os.fsencode(s))+1 for s in cmd[1:]) >= 4096:
            raise RuntimeError('UML kernel command line exceeds 4095 bytes')
        with (instance / f'guest-{run}.log').open('x') as log:
            guest = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     pass_fds=tuple(inherited), env={**os.environ, 'TMPDIR': str(tmp)},
                                     cwd=instance, start_new_session=True, preexec_fn=parent_death)
        guest_end.close()
        if a.publish_ports:
            port_guest.close()
            relay = PortDriver(port_parent, lambda command: control(instance, command),
                               (instance / f'ports-{run}.jsonl').open('x'), a.dns)
            relay.start()
        state.update({'guest_pid': guest.pid, 'network_pid': helper.pid, 'api_port': port,
                      'mconsole': str(uml_dir / run / 'mconsole'), 'command': cmd})
        write_state(instance, state)
        deadline = time.monotonic() + 120
        while not stopping:
            if guest.poll() is not None or helper.poll() is not None:
                raise RuntimeError('guest or network backend exited unexpectedly')
            if relay is not None and relay.error:
                raise RuntimeError('port driver failed: ' + relay.error)
            if any(fs.poll() is not None for fs, _, _, _ in filesystems):
                raise RuntimeError('shared filesystem backend exited unexpectedly')
            if state['phase'] == 'starting':
                try:
                    if api(port, '/_ping') == b'OK':
                        if config['shares']:
                            console = (instance/f'guest-{run}.log').read_text(errors='replace')
                            if any('HARDDIND_SHARE='+share_argument(s) not in console for s in config['shares']):
                                state['fixture_error'] = 'guest fixture did not confirm requested shared mounts'
                                break
                        if relay is not None and not relay.info_seen:
                            state['fixture_error'] = 'Docker did not connect to the requested port driver'
                            break
                        if a.mtu != 1500:
                            console = (instance / f'guest-{run}.log').read_text(errors='replace')
                            if f'HARDDIND_RUNTIME_MTU={a.mtu}' not in console:
                                state['fixture_error'] = 'guest fixture does not confirm requested MTU'
                                break
                        if a.memfd_ram or a.memfd_stub:
                            console = (instance / f'guest-{run}.log').read_text(errors='replace')
                            state['memory_backing'] = memfd_backing_evidence(console, a.memfd_ram, a.memfd_stub)
                            if not state['memory_backing']['verified']:
                                state['fixture_error'] = 'kernel did not confirm requested memfd backing'
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
        if relay is not None: relay.begin_shutdown()
        if guest is not None:
            forced = False
            if guest.poll() is None:
                try:
                    mconsole(instance, pathlib.Path(state['mconsole']), 'cad')
                    deadline = time.monotonic() + 45
                    try:
                        guest.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        try:
                            state['shutdown_diagnostics'] = shutdown_diagnostics(instance, state)
                        except Exception as error:
                            state['shutdown_diagnostics_error'] = str(error)
                        guest.wait(timeout=max(0, deadline - time.monotonic()))
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    state['shutdown_error'] = str(error)
                    forced = True
                    os.killpg(guest.pid, signal.SIGKILL)
                    guest.wait()
            state['guest_exit_code'] = guest.returncode
            state['guest_cleanup'] = cleanup_group(guest.pid)
            state['forced_shutdown'] = forced
        if relay is not None:
            state['port_driver'] = relay.stop()
            if state['port_driver']['error']:
                state.setdefault('error', 'port driver failed: ' + state['port_driver']['error'])
        if helper is not None:
            if helper.poll() is None:
                helper.terminate()
                try: helper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(helper.pid, signal.SIGKILL); helper.wait()
            state['network_exit_code'] = helper.returncode
            state['network_cleanup'] = cleanup_group(helper.pid)
        state['filesystem_cleanup'] = []
        for fs, path, share, _ in filesystems:
            if fs.poll() is None:
                fs.terminate()
                try: fs.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(fs.pid, signal.SIGKILL); fs.wait()
            item = {'share': share, 'exit_code': fs.returncode, **cleanup_group(fs.pid)}
            state['filesystem_cleanup'].append(item)
            path.unlink(missing_ok=True)
        for s in (guest_end, helper_end, port_parent, port_guest):
            if s is not None: s.close()
        ctl.unlink(missing_ok=True)
        clean = (guest is not None and state.get('guest_exit_code') == 0 and
                 state.get('guest_cleanup', {}).get('clean_shutdown') and
                 state.get('network_exit_code') == 0 and state.get('network_cleanup', {}).get('clean_shutdown') and
                 all(s['clean_shutdown'] and s['exit_code'] in (0, -signal.SIGTERM) for s in state['filesystem_cleanup']))
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
        s.add_argument('--publish-ports', action='store_true', help='experimental automatic IPv4 publishing; requires patched Moby fixture')
        s.add_argument('--memfd-ram', action='store_true', help='use experimental memfd RAM backing')
        s.add_argument('--memfd-stub', action='store_true', help='require sealed memfd userspace executable')
        s.add_argument('--gpu-async-wait', action='store_true', help='use experimental schedulable GPU waits; requires --gpu-control-experiment')
        s.add_argument('--gpu-control-experiment', action='store_true', help='enable experimental NVIDIA control queries; no CUDA or GPU mmap support yet')
        s.add_argument('--share', type=pathlib.Path, action='append', default=[], help='export this outer directory at the same guest path using virtiofs')
        s.add_argument('--virtiofsd', type=pathlib.Path, help='immutable filesystem backend binary for controlled comparisons')
        s.add_argument('--share-inline-flock', action='store_true', help='experimental inline nonblocking flock; requires a compatible backend')
        s.add_argument('--share-posix-locks', action='store_true', help='experimental cross-layer POSIX byte-range locks; requires a compatible backend')
        s.add_argument('--share-profile', action='store_true', help='diagnostic virtiofs timings; adds measurement overhead')
        s.add_argument('--share-cache', choices=('auto', 'never'), default='never', help='default: never; MAP_SHARED unsupported; auto can return stale external changes')
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
        if a.gpu_async_wait and not a.gpu_control_experiment:
            p.error('--gpu-async-wait requires --gpu-control-experiment')
        if not 1 <= a.cpus <= 64 or not a.memory[:-1].isdigit() or a.memory[-1:] not in ('M', 'G'):
            p.error('invalid resources')
        if not 576 <= a.mtu <= 65520: p.error('mtu must be between 576 and 65520')
        try: a.dns = str(ipaddress.IPv4Address(a.dns))
        except ipaddress.AddressValueError: p.error('dns must be an IPv4 resolver address')
        a.disk = a.disk or work / 'runtime.ext4'
        try: shares = share_specs(a.share)
        except (OSError, ValueError) as error: p.error(str(error))
        if (a.share_profile or a.share_inline_flock or a.share_posix_locks or a.virtiofsd) and not shares:
            p.error('filesystem backend options require --share')
        if shares: a.virtiofsd = (a.virtiofsd or work / 'virtiofsd').resolve()
        if a.action == 'serve': return serve(a, work, instance)
        if live(state):
            if state['phase'] != 'ready': raise RuntimeError('instance is still starting or stopping')
            requested = {'kernel': str(a.kernel.resolve()), 'disk': str(a.disk.resolve()), 'cpus': a.cpus, 'memory': a.memory, 'dns': a.dns, 'network_batch': a.network_batch, 'mtu': a.mtu, 'publish_ports': a.publish_ports, 'gpu_control_experiment': a.gpu_control_experiment, 'gpu_async_wait': a.gpu_async_wait,
              'memfd_ram': a.memfd_ram, 'memfd_stub': a.memfd_stub, 'shares': shares}
            if shares: requested.update(share_cache=a.share_cache, share_profile=a.share_profile,
                                        share_inline_flock=a.share_inline_flock,
                                        share_posix_locks=a.share_posix_locks,
                                        virtiofsd=str(a.virtiofsd))
            if any(state['config'].get(key, {'dns': '10.0.2.3', 'network_batch': 1, 'mtu': 1500, 'publish_ports': False, 'gpu_control_experiment': False, 'gpu_async_wait': False, 'memfd_ram': False, 'memfd_stub': False, 'shares': [], 'share_profile': False, 'share_inline_flock': False, 'share_posix_locks': False, 'virtiofsd': str((work / 'virtiofsd').resolve())}.get(key)) != value for key, value in requested.items()):
                raise RuntimeError('running instance uses different inputs; stop it or use another name')
            print(json.dumps(state, indent=2)); return 0
        cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), str(work), a.name, 'serve',
               '--kernel', str(a.kernel.resolve()), '--disk', str(a.disk.resolve()),
               '--cpus', str(a.cpus), '--memory', a.memory, '--dns', a.dns, '--network-batch', str(a.network_batch), '--mtu', str(a.mtu)]
        if a.publish_ports: cmd.append('--publish-ports')
        if a.gpu_control_experiment: cmd.append('--gpu-control-experiment')
        if a.gpu_async_wait: cmd.append('--gpu-async-wait')
        if a.memfd_ram: cmd.append('--memfd-ram')
        if a.memfd_stub: cmd.append('--memfd-stub')
        for share in shares: cmd += ['--share', share['target']]
        cmd += ['--share-cache', a.share_cache]
        if a.share_profile: cmd.append('--share-profile')
        if a.share_inline_flock: cmd.append('--share-inline-flock')
        if a.share_posix_locks: cmd.append('--share-posix-locks')
        if shares: cmd += ['--virtiofsd', str(a.virtiofsd)]
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
