"""Persistent stock Docker on UML. One instance = one guest with its own COW disk.

The guest runs the pinned dockerd; the outer Docker CLI reaches it through a
loopback port forwarded by the network backend. `start()` spawns a detached
supervisor (`serve`) that owns the guest, the network backend and optional
virtiofsd shares, and tears all of them down on stop.
"""
import base64
import ctypes
import fcntl
import http.client
import json
import os
import pathlib
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pipeline.guest_cleanup import cleanup_group, group_members
from pipeline.port_driver import PortDriver

# Boot parameters of the default execution core (see kernel/series).
# Guest RAM in a memfd (shmem): a file under TMPDIR would make the host write
# guest memory back to disk and stall exit in inode writeback.
# harddind_vdso_clock: clock_gettime and friends read a TSC vvar without trapping (patch 0031).
# queue_size/request_pages: a 1 MiB unaligned read fits one virtio-fs request (patch 0032).
# uml_faultaround*: map 32-page windows on sequential anonymous write faults (patches 0036, 0037).
CORE_ARGS = ('seccomp=on', 'seccomp_threads=1', 'seccomp_broker_cache=1', 'seccomp_spin=256', 'defer_ipi=1',
             'memfd_ram=1', 'harddind_vdso_clock=1', 'virtio_uml.queue_size=512', 'virtiofs.request_pages=512',
             'uml_faultaround=5', 'uml_faultaround_edges=1')
PROTECTED = ('/', '/proc', '/sys', '/dev', '/run', '/bin', '/sbin', '/lib', '/usr', '/var/lib/docker', '/var/lib/containerd')



def share_specs(paths):
    result = []
    for path in paths:
        target = pathlib.Path(os.path.abspath(path))
        if '\n' in str(target) or '\r' in str(target):
            raise ValueError('shared path cannot contain a newline')
        if any(target == pathlib.Path(p) or pathlib.Path(p).is_relative_to(target) for p in PROTECTED):
            raise ValueError('share would cover a required guest system path')
        source = target.resolve(strict=True)
        if not source.is_dir():
            raise ValueError('shared source must be a directory')
        if any(s['target'] == str(target) for s in result):
            raise ValueError('duplicate shared path')
        result.append({'source': str(source), 'target': str(target), 'tag': 'harddind' + str(len(result))})
    if len(result) > 8:
        raise ValueError('at most 8 shared directories per instance')
    return result


def share_argument(share):
    return share['tag'] + ':' + base64.b64encode(os.fsencode(share['target'])).decode()


def write_state(instance, state):
    tmp = instance / 'state.new'
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.replace(instance / 'state.json')


def read_state(instance):
    try:
        return json.loads((instance / 'state.json').read_text())
    except FileNotFoundError:
        return {}


def start_time(pid):
    try:
        fields = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def live(state):
    return bool(state.get('supervisor_start') and start_time(state['supervisor_pid']) == state['supervisor_start'])


def control(instance, command):
    """Send one command to the network backend control socket."""
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


def default_dns(resolv='/etc/resolv.conf'):
    """Outer resolvers if usable from the guest; else Docker's own fallback for
    loopback-only hosts (public resolvers). The guest reaches the outer host's
    resolver only through slirp, and Starpub's 127.0.0.11 stalls AAAA lookups."""
    try:
        servers = [line.split()[1] for line in open(resolv) if line.startswith('nameserver ') and len(line.split()) > 1]
    except OSError:
        servers = []
    usable = [s for s in servers if not s.startswith('127.') and s != '::1' and ':' not in s]
    return ','.join(usable[:3]) or '1.1.1.1,8.8.8.8'


def parse_cpus(text):
    cpus = set()
    for part in str(text).split(','):
        lo, _, hi = part.partition('-')
        cpus.update(range(int(lo), int(hi or lo) + 1))
    return cpus


def serve(instance):
    """Supervisor body; runs detached. Inputs come from instance/config.json."""
    os.umask(0o077)
    lock = (instance / 'instance.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = json.loads((instance / 'config.json').read_text())
    if config.get('hold_measure'):
        # Interactive guests block exclusive measurements for their whole life.
        from pipeline import locks
        write_state(instance, {'phase': 'queued', 'supervisor_pid': os.getpid(),
                               'supervisor_start': start_time(os.getpid())})
        measure = locks.acquire_shared()
    run = uuid.uuid4().hex[:12]
    state = {'phase': 'starting', 'run': run, 'supervisor_pid': os.getpid(),
             'supervisor_start': start_time(os.getpid()), 'config': config}
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
    cpuset = parse_cpus(config['cpuset']) if config.get('cpuset') else None
    # The guest, its network backend and virtiofsd are one virtual machine: same CPUs.
    def guest_setup():
        parent_death()
        if cpuset:
            os.sched_setaffinity(0, cpuset)
    ctl = instance / 'network.ctl'
    ctl.unlink(missing_ok=True)  # The exclusive instance lock is held.
    share_opts = config.get('share_options', {})
    try:
        for share in config['shares']:
            path = instance / (share['tag'] + '.sock')
            path.unlink(missing_ok=True)
            cmd = [config['virtiofsd'], '--socket-path', str(path), '--shared-dir', share['source'],
                   '--tag', share['tag'], '--sandbox', 'chroot', '--cache', share_opts.get('cache', 'never'),
                   '--inode-file-handles=never', '--xattr', '--posix-acl', '--announce-submounts', '--shared-flock',
                   # MAP_SHARED with cache=never (FUSE_DIRECT_IO_ALLOW_MMAP); mapped pages are
                   # not invalidated by outer writes, unlike a native bind mount.
                   '--allow-mmap',
                   '--thread-pool-size', '4']
            if share_opts.get('profile'): cmd.append('--profile-requests')
            if share_opts.get('inline_flock'): cmd.append('--inline-flock')
            if share_opts.get('inline_metadata'): cmd.append('--inline-metadata')
            if share_opts.get('notify_changes'): cmd.append('--notify-changes')
            if share_opts.get('posix_locks'): cmd.append('--shared-posix-locks')
            with (instance / f"{share['tag']}-{run}.log").open('x') as log:
                fs = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True, preexec_fn=guest_setup)
            filesystems.append((fs, path, share, cmd))
            until = time.monotonic() + 5
            while not path.exists():
                if fs.poll() is not None or time.monotonic() > until:
                    raise RuntimeError('virtiofsd did not start: ' + share['target'])
                time.sleep(.05)
        state['filesystems'] = [{'pid': fs.pid, 'share': share, 'command': cmd} for fs, _, share, cmd in filesystems]
        write_state(instance, state)
        guest_end, helper_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for s in (guest_end, helper_end):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        with (instance / f'network-{run}.log').open('x') as log:
            helper = subprocess.Popen([config['network_backend'], str(helper_end.fileno()), str(ctl),
                                       str(config['network_batch']), str(config['mtu']),
                                       *(['vnet'] if config.get('net_vnet') else [])],
                                      pass_fds=(helper_end.fileno(),), stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True, preexec_fn=guest_setup)
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
        mtu = config['mtu']
        vnet = int(bool(config.get('net_vnet')))
        cmd = [config['kernel'], f'umid={run}', 'uml_dir=uml/', f"mem={config['memory']}", f"ncpus={config['cpus']}",
               *CORE_ARGS,
               f"ubd0={instance / 'root.cow'},{config['disk']}", 'root=/dev/ubda', 'rw',
               'init=/sbin/harddind-runtime-init', 'con=null', 'con0=null,fd:1', 'ssl=null', 'panic=1',
               f"harddind_dns={config['dns']}", f'harddind_mtu={mtu}',
               f'vec0:transport=fd,fd={guest_end.fileno()},gro={vnet},vnet_hdr={vnet},mtu={mtu},mac=02:00:00:00:02:15',
               *config['kernel_args']]
        for _, path, share, _ in filesystems:
            cmd += [f'virtio_uml.device={path}:26', 'harddind_share=' + share_argument(share)]
        inherited = [guest_end.fileno()]
        if config['publish_ports']:
            port_parent, port_guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            inherited.append(port_guest.fileno())
            cmd += [f'ssl0=fd:{port_guest.fileno()}', 'harddind_ports=1']
        if sum(len(os.fsencode(s)) + 1 for s in cmd[1:]) >= 4096:
            raise RuntimeError('UML kernel command line exceeds 4095 bytes')
        with (instance / f'guest-{run}.log').open('x') as log:
            guest = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     pass_fds=tuple(inherited), env={**os.environ, 'TMPDIR': str(tmp)},
                                     cwd=instance, start_new_session=True, preexec_fn=guest_setup)
        guest_end.close()
        if config['publish_ports']:
            port_guest.close()
            relay = PortDriver(port_parent, lambda command: control(instance, command),
                               (instance / f'ports-{run}.jsonl').open('x'), config['dns'])
            relay.start()
        state.update({'guest_pid': guest.pid, 'network_pid': helper.pid, 'api_port': port,
                      'mconsole': str(uml_dir / run / 'mconsole'), 'command': cmd})
        write_state(instance, state)
        deadline = time.monotonic() + config['boot_timeout']
        console = instance / f'guest-{run}.log'
        booted = time.monotonic()
        markers = {'init': b'Run /sbin/harddind-runtime-init', 'dockerd': b'HARDDIND_RUNTIME_READY'}
        state['boot'] = {}
        while not stopping:
            if guest.poll() is not None or helper.poll() is not None:
                raise RuntimeError('guest or network backend exited unexpectedly')
            if relay is not None and relay.error:
                raise RuntimeError('port driver failed: ' + relay.error)
            if any(fs.poll() is not None for fs, _, _, _ in filesystems):
                raise RuntimeError('shared filesystem backend exited unexpectedly')
            if state['phase'] == 'starting':
                if len(state['boot']) < len(markers):
                    head = console.read_bytes()
                    for name, marker in markers.items():
                        if name not in state['boot'] and marker in head:
                            state['boot'][name] = round(time.monotonic() - booted, 2)
                try:
                    answered = api(port, '/_ping') == b'OK'
                except (OSError, http.client.HTTPException, RuntimeError):
                    answered = False
                if answered:
                    text = console.read_text(errors='replace')
                    if any('HARDDIND_SHARE=' + share_argument(s) not in text for s in config['shares']):
                        raise RuntimeError('guest did not confirm requested shared mounts')
                    if relay is not None and not relay.info_seen:
                        raise RuntimeError('Docker did not connect to the port driver')
                    if f'HARDDIND_RUNTIME_MTU={mtu}' not in text:
                        raise RuntimeError('guest did not confirm requested MTU')
                    state['boot']['api'] = round(time.monotonic() - booted, 2)
                    state['phase'] = 'ready'; state['ready_at'] = time.time(); write_state(instance, state)
                if state['phase'] != 'ready' and time.monotonic() > deadline:
                    raise RuntimeError('Docker API readiness timed out')
            time.sleep(0.1)
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
    return 0 if state['phase'] == 'stopped' else 1


def start(instance, *, kernel, disk, network_backend, client, virtiofsd=None, cpus=4, memory='4G',
          cpuset=None, kernel_args=(), shares=(), share_options=None, publish_ports=True,
          mtu=1500, dns=None, network_batch=1, net_vnet=False, boot_timeout=180, fresh=True, hold_measure=False):
    """Boot a guest in `instance` and wait until dockerd answers.

    fresh=True requires a new instance; fresh=False reboots on the existing COW disk."""
    instance = pathlib.Path(instance)
    if len(str(instance / 'network.ctl').encode()) > 100:
        raise ValueError('instance path too long for Unix sockets')
    instance.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = read_state(instance)
    if live(state):
        raise RuntimeError(f'instance {instance.name} is already running')
    if fresh and (instance / 'root.cow').exists():
        raise RuntimeError(f'instance {instance.name} already has a disk; remove it or use a new name')
    shares = share_specs(shares)
    if shares and not virtiofsd:
        raise ValueError('shares require a virtiofsd binary')
    config = {'kernel': str(kernel), 'disk': str(disk), 'network_backend': str(network_backend),
              'client': str(client), 'virtiofsd': str(virtiofsd) if virtiofsd else None,
              'cpus': cpus, 'memory': memory, 'cpuset': cpuset, 'kernel_args': list(kernel_args),
              'shares': shares, 'share_options': share_options or {}, 'publish_ports': publish_ports,
              'mtu': mtu, 'dns': dns or default_dns(), 'network_batch': network_batch, 'net_vnet': net_vnet, 'boot_timeout': boot_timeout,
              'hold_measure': hold_measure}
    (instance / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), 'serve', str(instance)]
    with (instance / 'supervisor.log').open('a') as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    until = time.monotonic() + boot_timeout + 30
    queued = False
    while time.monotonic() < until:
        state = read_state(instance)
        if state.get('supervisor_pid') == proc.pid and state.get('phase') == 'queued':
            if not queued:
                print(f'{instance.name}: waiting for a running benchmark to finish', file=sys.stderr, flush=True)
                queued = True
            until = time.monotonic() + boot_timeout + 30
        elif state.get('supervisor_pid') == proc.pid:
            if state.get('phase') == 'ready':
                return state
            if state.get('phase') in ('stopping', 'stopped', 'failed'):
                proc.wait()
                raise RuntimeError('guest failed to start: ' + read_state(instance).get('error', 'unknown error'))
        if proc.poll() is not None:
            raise RuntimeError('supervisor exited during startup; see ' + str(instance / 'supervisor.log'))
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError('guest startup timed out')


def stop(instance, timeout=90):
    instance = pathlib.Path(instance)
    state = read_state(instance)
    if live(state):
        os.kill(state['supervisor_pid'], signal.SIGTERM)
        until = time.monotonic() + timeout
        while live(state) and time.monotonic() < until:
            time.sleep(0.2)
        if live(state):
            raise RuntimeError('supervisor did not finish shutdown')
    return read_state(instance)


def remove(instance):
    """Delete a stopped instance, keeping nothing."""
    instance = pathlib.Path(instance)
    if live(read_state(instance)):
        raise RuntimeError('instance is still running')
    shutil.rmtree(instance, ignore_errors=True)


def docker_env(instance):
    """argv prefix and environment for the outer Docker CLI talking to this guest."""
    instance = pathlib.Path(instance)
    state = read_state(instance)
    if not live(state) or state.get('phase') != 'ready':
        raise RuntimeError(f'instance {instance.name} is not ready')
    client = pathlib.Path(state['config']['client'])
    config = instance / 'docker-config'
    config.mkdir(exist_ok=True, mode=0o700)
    config_file = config / 'config.json'
    if not config_file.exists():
        config_file.write_text(json.dumps({'cliPluginsExtraDirs': [str(client / 'cli-plugins')]}))
    env = {k: v for k, v in os.environ.items() if k not in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
    env.update(DOCKER_HOST=f"tcp://127.0.0.1:{state['api_port']}", DOCKER_CONFIG=str(config))
    return [str(client / 'ld-musl-x86_64.so.1'), str(client / 'docker')], env


if __name__ == '__main__':
    if sys.argv[1:2] != ['serve'] or len(sys.argv) != 3:
        raise SystemExit('usage: instance.py serve INSTANCE_DIR')
    raise SystemExit(serve(pathlib.Path(sys.argv[2])))
