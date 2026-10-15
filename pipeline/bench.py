"""Paired, verified workloads from one rootfs; disk reads include warm page cache.

Native writes live below run_dir, never in the immutable store. No mounts or
/proc are needed. Network clients time the transfer, including receiver ACK.
"""
import contextlib
import hashlib
import io
import json
import math
import os
import pathlib
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import time

from pipeline import build, config

TARGETS = json.loads((config.REPO / 'bench/targets.json').read_text())['metrics']
METRICS = [name for name, spec in TARGETS.items() if spec.get('suite') != 'gpu']


def cpuset(value):
    result = set()
    for item in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', item):
            raise ValueError(f'invalid CPU set: {value}')
        bounds = [int(x) for x in item.split('-')]
        first, last = bounds[0], bounds[-1]
        if last < first:
            raise ValueError(f'invalid CPU range: {item}')
        result.update(range(first, last + 1))
    return result


def parse_sample(output):
    """Read the last JSON line, rejecting NaN, bools and nonpositive samples."""
    for line in reversed(output.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        value = obj.get('value') if isinstance(obj, dict) else None
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'invalid benchmark sample: {line[:200]}')
        return float(value)
    raise ValueError('workload did not print a JSON sample')


def metric_info(name):
    if name.startswith(('docker.', 'app.')):
        return 'seconds', False
    if name.startswith('mem.'):
        return 'GB/s', True
    if name.startswith('disk.') or name.endswith('_stream'):
        return 'MB/s', True
    if name.endswith('_rr') or name == 'micro.pipe':
        return 'round trips/s', True
    return 'ops/s', True


def check_build_cache(output):
    """Require both COPY and RUN to be cached, not merely the base image."""
    steps = re.findall(r'^(#\d+) \[[^\n]+\] (COPY marker /marker|RUN test[^\n]*)$', output, re.M)
    if len(steps) == 2 and all(re.search(r'^' + re.escape(step) + r' CACHED$', output, re.M)
                               for step, _ in steps):
        return
    raise RuntimeError('rebuild did not cache COPY and RUN: ' + output[-500:])


def small_archive(path, count=20000):
    with tarfile.open(path, 'w') as archive:
        for i in range(count):
            data = f'harddind-file-{i:05d}\n'.encode()
            entry = tarfile.TarInfo(f'{i:05d}'); entry.size = len(data); entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))


def bench_root(alpine_tar=None):
    """Build once from checksum-locked APKs, then verify the complete manifest."""
    lock = build.LOCK
    inputs = sorted((config.REPO / 'bench').glob('*.c')) + [config.REPO / 'bench/common.h', pathlib.Path(__file__)]
    compiler = subprocess.run(['gcc', '--version'], check=True, capture_output=True, text=True).stdout
    pins = {k: v for k, v in lock.items() if k.startswith('bench_')}
    key = build.key_of('bench-root/1', lock['alpine'], pins, compiler, *inputs)
    alpine = pathlib.Path(alpine_tar) if alpine_tar else build.fetch('alpine')
    if build.file_sha(alpine) != lock['alpine']['sha256']:
        raise RuntimeError('Alpine minirootfs checksum mismatch')

    def builder(tmp, _final):
        root = tmp / 'root'; root.mkdir()
        log = tmp / 'build.log'
        build.run(['tar', '-xf', alpine, '-C', root], log_file=log)
        for name, minor in [('null', 3), ('zero', 5), ('urandom', 9)]:
            path = root / 'dev' / name
            path.unlink(missing_ok=True)
            os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(1, minor))
            path.chmod(0o666)
        packages = root / 'packages'; packages.mkdir()
        archives = []
        for name in sorted(pins):
            if name.startswith('bench_apk_'):
                path = build.fetch(name)
                shutil.copy2(path, packages / path.name)
                archives.append('/packages/' + path.name)
        # All transitive APKs are local and hashed; apk still checks signatures.
        build.run(['chroot', root, 'apk', 'add', '--no-network', '--no-cache', *lock['bench_packages'], *archives],
                  log_file=log, timeout=300)
        actual = set(subprocess.run(['chroot', root, 'apk', 'info', '-v'], check=True,
                                    capture_output=True, text=True).stdout.split())
        expected = set(lock['bench_package_manifest'])
        if actual != expected:
            raise RuntimeError(f'bench APK drift: missing {sorted(expected - actual)}, unexpected {sorted(actual - expected)}')
        shutil.rmtree(packages)
        dest = root / 'bench'; dest.mkdir(); (root / 'work').mkdir()
        for name in ('workload', 'network', 'true'):
            build.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                       config.REPO / 'bench' / f'{name}.c', '-o', dest / name], log_file=log)
        shutil.copy2(build.fetch('bench_lua'), dest / 'lua.tar.gz')
        (dest / 'verify.lua').write_text('assert(6 * 7 == 42)\n')
        small_archive(dest / 'small.tar')
        small_archive(dest / 'small-quick.tar', 2000)
        build.run(['tar', '--sort=name', '--mtime=@0', '--numeric-owner', '-cf', tmp / 'rootfs.tar', '-C', root, '.'], log_file=log)
    return build.artifact('bench-root', key, builder)


class Workloads:
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = ctx.run_dir / 'bench-native'
        self.cpus = cpuset(ctx.bench_cpus)
        self.image = None
        self.serial = 0
        self.log_path = ctx.run_dir / 'bench.log'
        self.deadline = time.monotonic() + (360 if ctx.quick else 1500)

    def timeout(self, requested):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('benchmark wall-time budget exhausted')
        return min(requested, remaining)

    def record(self, argv, stdout='', stderr='', returncode=0):
        with self.log_path.open('a') as out:
            out.write(json.dumps({'time': time.time(), 'argv': list(map(str, argv)), 'returncode': returncode,
                                  'stdout': stdout, 'stderr': stderr}) + '\n')

    def prepare(self):
        artifact = bench_root(self.ctx.artifacts.get('alpine_tar'))
        # A cold artifact build is not part of the measurement time budget.
        self.deadline = time.monotonic() + (360 if self.ctx.quick else 1500)
        self.image = 'hd-bench:' + artifact.name
        if self.root.exists():
            raise RuntimeError(f'native root already exists: {self.root}')
        build.run(['cp', '-a', artifact / 'root', self.root])
        argv, env = self.ctx.docker_cmd()
        with open(artifact / 'rootfs.tar', 'rb') as archive:
            self.process([*argv, 'import', '-', self.image], stdin=archive, env=env, timeout=180)
        return {'root_key': artifact.name, 'image': self.image, 'native_root': str(self.root),
                'quick': self.ctx.quick, 'warmups_per_side': 1,
                'timing': 'inside workload; Docker control plane uses outer CLI wall time'}

    def process(self, argv, timeout=120, **kwargs):
        timeout = self.timeout(timeout)
        proc = subprocess.Popen(list(map(str, argv)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True, **kwargs)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            self.record(argv, stdout, stderr, proc.returncode)
            raise RuntimeError(f'command timed out after {timeout}s: {argv[:4]}') from None
        self.record(argv, stdout, stderr, proc.returncode)
        if proc.returncode:
            raise RuntimeError(f'command exited {proc.returncode}: {argv[:4]}: {stderr[-800:]}')
        return stdout

    def native(self, *args, timeout=120):
        return self.process(['chroot', self.root, *args], timeout=timeout,
                            preexec_fn=lambda: os.sched_setaffinity(0, self.cpus))

    def docker(self, *args, timeout=120, combined=False):
        completed = self.ctx.docker(*args, timeout=self.timeout(timeout))
        self.record(['docker', *args], completed.stdout, completed.stderr, completed.returncode)
        if completed.returncode:
            raise RuntimeError(f'docker {args[:3]} exited {completed.returncode}: {completed.stderr[-800:]}')
        return (completed.stdout + ('\n' + completed.stderr if combined else '')).strip()

    def cleanup(self, *args):
        """Cleanup remains possible after the measurement deadline expires."""
        completed = self.ctx.docker(*args, timeout=20)
        self.record(['docker', *args], completed.stdout, completed.stderr, completed.returncode)
        if completed.returncode:
            raise RuntimeError(f'cleanup {args[:3]}: {completed.stderr[-400:]}')

    def unique(self, label):
        self.serial += 1
        digest = hashlib.sha256(self.ctx.run_id.encode()).hexdigest()[:10]
        return f'hdb-{digest}-{label}-{self.serial}'

    @contextlib.contextmanager
    def container(self, options=(), command=('/bin/sleep', '86400')):
        name = self.unique('container')
        failed = False
        try:
            self.docker('run', '-d', '--name', name, *options, self.image, *command)
            yield name
        except Exception:
            failed = True
            raise
        finally:
            # Also reap a container created before a timed-out run command.
            try:
                self.cleanup('rm', '-f', name)
            except Exception as error:
                if not failed:
                    raise
                self.ctx.log(f'bench cleanup {name}: {error}')

    @contextlib.contextmanager
    def native_server(self):
        argv = ['chroot', str(self.root), '/bench/network', 'server', '127.0.0.1', '0']
        stderr = (self.ctx.run_dir / self.unique('server.log')).open('w+')
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=stderr, text=True,
                                start_new_session=True, preexec_fn=lambda: os.sched_setaffinity(0, self.cpus))
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                if not selector.select(10):
                    raise RuntimeError('native network server did not start')
            port = json.loads(proc.stdout.readline())['port']
            yield port
            if proc.poll() is not None:
                raise RuntimeError(f'native network server exited {proc.returncode}')
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
            proc.stdout.close(); stderr.seek(0)
            self.record(argv, stderr=stderr.read(), returncode=proc.returncode); stderr.close()

    def guest_server_ready(self, name):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            output = self.docker('logs', name)
            if any(line.startswith('{"port":') for line in output.splitlines()):
                return
            if self.docker('inspect', '-f', '{{.State.Running}}', name) != 'true':
                raise RuntimeError(f'network server {name} exited: {output}')
            time.sleep(0.1)
        raise RuntimeError('guest network server did not start')

    @contextlib.contextmanager
    def network_samples(self, name):
        mode = 'rr' if name.endswith('_rr') else 'stream'
        seconds = '0.2' if self.ctx.quick else '1.5'
        with contextlib.ExitStack() as stack:
            native_port = stack.enter_context(self.native_server())
            native = lambda: self.native('/bench/network', 'client', '127.0.0.1', str(native_port), mode, seconds)
            if name.startswith('net.egress'):
                guest = lambda: self.guest('/bench/network', 'client', '10.0.2.2', str(native_port), mode, seconds)
            else:
                options = []
                if name.startswith('net.bridge'):
                    network = self.unique('net')
                    self.docker('network', 'create', network)
                    stack.callback(self.cleanup, 'network', 'rm', network)
                    options = ['--network', network]
                else:
                    options = ['-p', '127.0.0.1::9000']
                server = stack.enter_context(self.container(options, ('/bench/network', 'server', '0.0.0.0', '9000')))
                self.guest_server_ready(server)
                if name.startswith('net.bridge'):
                    address = self.docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', server)
                    guest = lambda: self.guest('/bench/network', 'client', address, '9000', mode, seconds,
                                               options=options)
                else:
                    port = published_port(self.docker('port', server, '9000/tcp'))
                    guest = lambda: self.native('/bench/network', 'client', '127.0.0.1', str(port), mode, seconds)
            yield native, guest

    def guest(self, *command, options=(), timeout=120):
        name = self.unique('sample')
        try:
            return self.docker('run', '--rm', '--name', name, *options, self.image, *command, timeout=timeout)
        except Exception:
            # CLI timeout does not cancel the daemon's running container.
            try:
                self.cleanup('rm', '-f', name)
            except Exception as cleanup:
                self.ctx.log(f'bench cleanup {name}: {cleanup}')
            raise

    @contextlib.contextmanager
    def samples(self, name):
        if name.startswith('net.'):
            with self.network_samples(name) as pair:
                yield pair
        elif name.startswith('docker.'):
            with self.control_samples(name) as sample:
                yield None, sample
        else:
            command = ['/bench/workload', name, str(int(self.ctx.quick))]
            with contextlib.ExitStack() as stack:
                options = []
                if name == 'disk.volume_write':
                    volume = self.unique('volume'); self.docker('volume', 'create', volume)
                    stack.callback(self.cleanup, 'volume', 'rm', volume)
                    options = ['--mount', f'type=volume,src={volume},dst=/work']
                yield (lambda: self.native(*command)), (lambda: self.guest(*command, options=options))

    @contextlib.contextmanager
    def control_samples(self, name):
        if name == 'docker.run':
            yield lambda: self.guest('/bin/true')
        elif name == 'docker.exec':
            with self.container() as container:
                yield lambda: self.docker('exec', container, '/bin/true')
        elif name == 'docker.lifecycle':
            def lifecycle():
                container = self.unique('lifecycle')
                try:
                    self.docker('create', '--name', container, self.image, '/bin/sleep', '86400')
                    self.docker('start', container)
                    self.docker('stop', '-t', '0', container)
                    self.docker('rm', container)
                except Exception:
                    self.cleanup('rm', '-f', container)
                    raise
            yield lifecycle
        else:
            path = self.ctx.run_dir / 'bench-build'; path.mkdir(exist_ok=True)
            (path / 'Dockerfile').write_text(f'FROM {self.image}\nCOPY marker /marker\nRUN test "$(cat /marker)" = hd-bench\n')
            (path / 'marker').write_text('hd-bench\n')
            image = self.unique('build') + ':test'
            def cached_build():
                output = self.docker('build', '--progress=plain', '--network=default', '-t', image, path,
                                     timeout=90, combined=True)
                check_build_cache(output)
            try:
                self.docker('build', '--network=default', '-t', image, path, timeout=180)
                yield cached_build
            finally:
                self.cleanup('image', 'rm', image)

    def close(self):
        if self.root.exists():
            shutil.rmtree(self.root)


def published_port(output):
    for line in output.splitlines():
        match = re.fullmatch(r'(?:127\.0\.0\.1|0\.0\.0\.0):(\d+)', line.strip())
        if match and 0 < int(match[1]) <= 65535:
            return int(match[1])
    raise ValueError(f'no published IPv4 port: {output!r}')


def measure_pair(ctx, runner, name, metric, host_load, pairs):
    with runner.samples(name) as (native, guest):
        control = name.startswith('docker.')
        def sample(fn):
            if control:
                start = time.monotonic(); fn(); value = time.monotonic() - start
                if value <= 0:
                    raise ValueError('nonpositive control-plane duration')
                return value
            return parse_sample(fn())
        if native:
            sample(native)
        sample(guest)
        for _ in range(pairs):
            if native:
                metric['native'].append(sample(native))
            metric['guest'].append(sample(guest))
            host_load.append([time.time(), os.getloadavg()[0]])


def run(ctx):
    pairs = 2 if ctx.quick else 5
    result = {'schema': 'hd.bench/1', 'cpus': ctx.bench_cpus, 'pairs': pairs, 'host_load': [], 'metrics': {}}
    selected = [name for name in METRICS if ctx.selected(name)]
    if not selected:
        return result
    for name in selected:
        unit, higher = metric_info(name)
        result['metrics'][name] = {'unit': unit, 'higher_is_better': higher, 'native': [], 'guest': [], 'error': None}
    runner = None
    def close():
        if runner is None:
            return
        try:
            runner.close()
        except Exception as error:
            message = f'cleanup: {type(error).__name__}: {error}'
            ctx.log(f'bench {message}')
            for metric in result['metrics'].values():
                metric['error'] = metric['error'] or message
    try:
        runner = Workloads(ctx)
        detail = runner.prepare()
    except Exception as error:
        for metric in result['metrics'].values():
            metric['error'] = f'prepare: {type(error).__name__}: {error}'
        close()
        return result
    try:
        for name, metric in result['metrics'].items():
            metric['detail'] = dict(detail)
            if name == 'disk.seq_read':
                metric['detail']['read_policy'] = 'write+fsync then warm page-cache read with byte comparison; no cache drop'
            ctx.log(f'bench {name}: warmup + {pairs} pairs')
            try:
                measure_pair(ctx, runner, name, metric, result['host_load'], pairs)
            except Exception as error:
                metric['error'] = f'{type(error).__name__}: {error}'
                ctx.log(f'bench {name}: {metric["error"]}')
    finally:
        close()
    return result
