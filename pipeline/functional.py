"""Docker command effects, driven by the outer stock CLI."""
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import tarfile
import threading
import time

from pipeline import build, config

FIXTURES = config.REPO / 'suites/functional'
BASE = 'hd-base:latest'
HELPERS = 'hd-functional:local'
FLAGS = ['-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread']
CHECKS = []


class NotTested(Exception):
    pass


def require(value, detail):
    if not value:
        raise AssertionError(detail)


def check(name, covers, timeout=90, base=True, helpers=False):
    def register(fn):
        CHECKS.append((name, covers.split('|'), timeout, base, helpers, fn))
        return fn
    return register


def coverage(checks, commands=None):
    commands = commands if commands is not None else [
        s.strip() for s in (FIXTURES / 'commands.txt').read_text().splitlines()
        if s.strip() and not s.startswith('#')]
    covered = {c for row in checks if row['status'] == 'pass' for c in row['covers']}
    return {'commands': sorted(set(commands)), 'covered': sorted(covered & set(commands)),
            'missing': sorted(set(commands) - covered)}


@contextlib.contextmanager
def deadline(seconds):
    """Bound callbacks and guest restart as well as CLI calls."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('functional suite requires the main thread for timeout enforcement')
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(_sig, _frame):
        raise TimeoutError(f'check deadline exceeded ({seconds}s)')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, old_timer[0] - (time.monotonic() - started)), old_timer[1])


def helper_artifact():
    sources = sorted((FIXTURES / 'helpers').glob('*.c'))
    compiler = subprocess.run(['gcc', '--version'], capture_output=True, text=True, check=True, timeout=10).stdout
    libraries = []
    for name in ('libc.a', 'libgcc.a', 'libgcc_eh.a', 'libpthread.a', 'crt1.o', 'crti.o', 'crtn.o', 'crtbeginT.o', 'crtend.o'):
        path = subprocess.run(['gcc', '-print-file-name=' + name], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        libraries.append(pathlib.Path(path).resolve())
    key = build.key_of('functional-helpers/2', FLAGS, compiler, *libraries, *sources)
    def compile_helpers(tmp, _final):
        (tmp / 'build.log').write_text(compiler)
        for source in sources:
            build.run(['gcc', *FLAGS, source, '-o', tmp / source.stem], log_file=tmp / 'build.log', timeout=90)
            elf = subprocess.run(['readelf', '-l', tmp / source.stem], capture_output=True, text=True, check=True, timeout=10)
            require('INTERP' not in elf.stdout, 'helper is dynamically linked: ' + source.stem)
    return build.artifact('functional-helpers', key, compile_helpers)


class Suite:
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = ctx.run_dir / 'functional'
        self.root.mkdir(parents=True, exist_ok=True)
        self.prefix = 'hf-' + hashlib.sha256(str(ctx.run_dir).encode()).hexdigest()[:8]
        self.ready, self.errors, self.images = set(), {}, []
        self.bins = None
        self.end = time.monotonic() + 1170

    def prepare(self, case, helpers=False):
        kind = 'helpers' if helpers else 'base'
        if kind in self.errors:
            raise NotTested(f'{kind} fixture unavailable: {self.errors[kind]}')
        if kind in self.ready:
            return
        try:
            if helpers:
                self.prepare(case)
                self.bins = helper_artifact()
                case.write('helper artifact: ' + str(self.bins))
                shutil.copy2(self.bins / 'build.log', self.root / 'helpers.build.log')
                (self.root / 'helpers.json').write_text(json.dumps({p.name: build.file_sha(p)
                    for p in self.bins.iterdir() if p.is_file() and p.name not in ('build.log', '.complete')}, indent=2) + '\n')
                dest = self.root / 'image'
                dest.mkdir(exist_ok=True)
                for source in (FIXTURES / 'helpers').glob('*.c'):
                    shutil.copy2(self.bins / source.stem, dest / source.stem)
                shutil.copy2(self.ctx.artifacts['network_probe'], dest / 'network_probe')
                shutil.copy2(FIXTURES / 'Dockerfile', dest / 'Dockerfile')
                self.images.append(HELPERS)
                case.d('build', '--pull=false', '--label', 'hd.check=fixture.helpers', '-t', HELPERS, dest, timeout=180)
            else:
                self.images.append(BASE)
                case.d('import', '--change', 'LABEL hd.check=fixture.base', self.ctx.artifacts['alpine_tar'], BASE)
                require(case.d('image', 'inspect', '-f', '{{index .Config.Labels "hd.check"}}', BASE).strip()
                        == 'fixture.base', 'base import label missing')
            self.ready.add(kind)
        except Exception as error:
            self.errors[kind] = str(error)
            raise


class Case:
    def __init__(self, suite, name, timeout):
        self.suite, self.ctx, self.id = suite, suite.ctx, name
        self.end = time.monotonic() + timeout
        self.label = 'hd.check=' + name
        self.name = suite.prefix + '-' + name.replace('_', '-').replace('.', '-')
        self.root = suite.root / name
        self.root.mkdir(exist_ok=True)
        self.log = (suite.root / (name + '.log')).open('w')
        self.cleanups, self.processes = [], []
        self.serial = 0
        self.has_images = False

    def write(self, text):
        self.log.write(str(text) + '\n')
        self.log.flush()

    def remaining(self, maximum=120):
        value = min(maximum, self.end - time.monotonic())
        if value <= 0:
            raise TimeoutError('check deadline exceeded')
        return value

    def d(self, *args, input=None, expected=0, timeout=60):
        self.write('$ docker ' + shlex.join(map(str, args)))
        try:
            result = self.ctx.docker(*args, input=input, timeout=self.remaining(timeout))
        except Exception as error:
            cause = error if isinstance(error, subprocess.TimeoutExpired) else error.__cause__
            if isinstance(cause, subprocess.TimeoutExpired):
                for text in (cause.stdout, cause.stderr):
                    if text:
                        self.write(text.decode(errors='replace') if isinstance(text, bytes) else text)
            raise
        self.write(result.stdout + result.stderr + f'\n[exit={result.returncode}]')
        require(result.returncode != 0 if expected == 'failure' else result.returncode == expected,
                f'docker {args[:3]} exited {result.returncode}, expected {expected}: '
                + (result.stdout + result.stderr)[-800:])
        if result.returncode == 0:
            self.forget_removed(args)
        return result.stdout + result.stderr if expected == 'failure' else result.stdout

    def absent(self, *args):
        output = self.d(*args, expected='failure')
        require(re.search(r'(?i)no such|not found|does not exist', output),
                'absence not established: ' + output[-500:])

    def forget_removed(self, args):
        args = tuple(map(str, args))
        aliases = {'rmi': ('image', 'rm'), 'rm': ('rm',)}
        prefix = aliases.get(args[0], args[:2])
        if prefix not in {('rm',), ('image', 'rm'), ('volume', 'rm'), ('network', 'rm'),
                          ('context', 'rm'), ('service', 'rm'), ('secret', 'rm'), ('config', 'rm')}:
            if args[:2] == ('swarm', 'leave'):
                self.cleanups = [a for a in self.cleanups if a[:2] != ('swarm', 'leave')]
            return
        targets = {a for a in args[1:] if not a.startswith('-')}
        self.cleanups = [a for a in self.cleanups
                         if not (a[:len(prefix)] == prefix and a[-1] in targets)]

    def defer(self, *args):
        self.cleanups.append(tuple(map(str, args)))

    def object(self, kind, suffix=''):
        name = self.name + ('-' + suffix if suffix else '')
        commands = {'container': ('rm', '-fv'), 'image': ('image', 'rm', '-f'),
                    'volume': ('volume', 'rm', '-f'), 'network': ('network', 'rm'),
                    'context': ('context', 'rm', '-f'), 'service': ('service', 'rm'),
                    'secret': ('secret', 'rm'), 'config': ('config', 'rm')}
        if kind == 'image':
            name += ':local'
            self.has_images = True
        self.defer(*commands[kind], name)
        return name

    def container(self, *command, opts=(), image=BASE, detached=False, create=False, suffix=None):
        self.serial += 1
        name = self.object('container', suffix or str(self.serial))
        self.d('create' if create else 'run', '--name', name, '--label', self.label,
               *(['-d'] if detached and not create else []), *opts, image, *command)
        return name

    def output(self, *command, opts=(), image=BASE, input=None, expected=0):
        self.serial += 1
        name = self.object('container', str(self.serial))
        output = self.d('run', '--rm', '--name', name, '--label', self.label, *opts,
                        image, *command, input=input, expected=expected)
        self.forget_removed(('rm', name))
        return output

    def shell(self, script, **kwargs):
        return self.output('sh', '-ec', script, **kwargs)

    def inspect(self, name):
        return json.loads(self.d('inspect', name))[0]

    def volume(self, suffix='data'):
        name = self.object('volume', suffix)
        self.d('volume', 'create', '--label', self.label, name)
        return name

    def network(self, *opts, suffix='net'):
        name = self.object('network', suffix)
        self.d('network', 'create', '--label', self.label, *opts, name)
        return name

    def eventually(self, predicate, timeout=20):
        end = time.monotonic() + self.remaining(timeout)
        last = None
        while time.monotonic() < end:
            try:
                last = predicate()
                if last:
                    return last
            except (OSError, AssertionError) as error:
                last = str(error)
            time.sleep(.2)
        raise AssertionError(f'condition did not become true: {last}')

    def spawn(self, *args, stdin=subprocess.PIPE, stdout=None, env=None, command=None, cwd=None):
        prefix, default_env = self.ctx.docker_cmd()
        argv = command or [*prefix, *map(str, args)]
        self.write('$ ' + shlex.join(map(str, argv)))
        process = subprocess.Popen(argv, env=env or default_env, stdin=stdin,
                                   stdout=stdout if stdout is not None else subprocess.PIPE,
                                   stderr=self.log, cwd=cwd, bufsize=0, start_new_session=True)
        self.processes.append(process)
        return process

    def cleanup(self):
        self.end = time.monotonic() + 35
        errors = []
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as error:
                    errors.append(str(error))
            for stream in (process.stdin, process.stdout):
                if stream and hasattr(stream, 'close'):
                    stream.close()
        for args in reversed(self.cleanups):
            try:
                result = self.ctx.docker(*args, timeout=self.remaining(6))
                output = result.stdout + result.stderr
                self.write('$ docker ' + shlex.join(args) + '\n' + output)
                if result.returncode and not re.search(r'(?i)no such|not found|does not exist|not part of a swarm', output):
                    errors.append(output[-300:])
            except Exception as error:
                errors.append(str(error))
        if self.has_images:
            try:
                self.d('image', 'prune', '-af', '--filter', 'label=' + self.label, timeout=10)
            except Exception as error:
                errors.append(str(error))
        shutil.rmtree(self.ctx.share / self.id, ignore_errors=True)
        if errors:
            raise RuntimeError('cleanup: ' + '; '.join(errors))


def run_checks(suite, specs):
    rows = []
    for name, covers, seconds, base, helpers, fn in specs:
        row = {'id': name, 'status': 'not_tested', 'seconds': 0.0, 'covers': covers, 'detail': ''}
        rows.append(row)
        if not suite.ctx.selected(name) or time.monotonic() >= suite.end:
            row['detail'] = 'not selected' if not suite.ctx.selected(name) else 'suite time budget exhausted'
            (suite.root / (name + '.log')).write_text(row['detail'] + '\n')
            continue
        seconds = min(seconds, suite.end - time.monotonic())
        started = time.monotonic()
        case = Case(suite, name, seconds)
        suite.ctx.log('functional ' + name)
        try:
            with deadline(seconds):
                if base:
                    suite.prepare(case, helpers)
                fn(case)
            row['status'] = 'pass'
        except NotTested as error:
            row['detail'] = str(error)[-800:]
        except Exception as error:
            case.write(f'{type(error).__name__}: {error}')
            row.update(status='fail', detail=(suite.root / (name + '.log')).read_text()[-800:])
        finally:
            try:
                with deadline(40):
                    case.cleanup()
            except Exception as error:
                row.update(status='fail', detail=(row['detail'] + '\n' + str(error))[-800:])
            row['seconds'] = round(time.monotonic() - started, 3)
            case.write(json.dumps(row))
            case.log.close()
        suite.ctx.log(f'functional {name}: {row["status"]} ({row["seconds"]:.1f}s)')
    return rows


def run(ctx):
    suite = Suite(ctx)
    rows = []
    try:
        rows = run_checks(suite, CHECKS)
    finally:
        if suite.images:
            with (suite.root / 'fixture.cleanup.log').open('w') as log:
                try:
                    result = ctx.docker('image', 'rm', '-f', *reversed(suite.images), timeout=30)
                    log.write(result.stdout + result.stderr)
                    require(result.returncode == 0, result.stdout + result.stderr)
                except Exception as error:
                    rows.append({'id': 'fixture.cleanup', 'status': 'fail', 'seconds': 0.0,
                                 'covers': [], 'detail': str(error)[-800:]})
    return {'schema': 'hd.test/1', 'checks': rows, 'coverage': coverage(rows)}


@check('system.version_info', 'version|system info', base=False)
def version_info(c):
    version = json.loads(c.d('version', '--format', '{{json .}}'))
    info = json.loads(c.d('info', '--format', '{{json .}}'))
    require(version['Server']['Version'] and version['Client']['Version'], version)
    require(info['NCPU'] >= 2 and info['MemTotal'] > 0 and info['ID'], info)
    require(any('seccomp' in s for s in info['SecurityOptions']), info['SecurityOptions'])


@check('image.import', 'image import|image inspect|run')
def image_import(c):
    require(c.output('cat', '/etc/alpine-release').strip() == build.LOCK['alpine']['version'], 'imported rootfs version differs')


@check('container.lifecycle', 'container create|container start|container stop|container restart|container kill|container rm|container inspect|container exec|container logs|container ps')
def lifecycle(c):
    n = c.container('sh', '-ec', 'echo started; exec sleep 300', create=True)
    ident = c.inspect(n)['Id']
    c.d('start', n)
    require(ident in c.d('ps', '--no-trunc', '-q', '--filter', 'label=' + c.label), 'running container absent')
    c.d('exec', n, 'sh', '-ec', 'printf persisted >/marker')
    c.eventually(lambda: 'started' in c.d('logs', n))
    c.d('stop', '-t', '1', n)
    require(not c.inspect(n)['State']['Running'], 'stop left container running')
    c.d('start', n)
    before = c.inspect(n)['State']['StartedAt']
    c.d('restart', '-t', '1', n)
    after = c.inspect(n)
    require(after['Id'] == ident and after['State']['Running'] and after['State']['StartedAt'] != before, after['State'])
    require(c.d('exec', n, 'cat', '/marker') == 'persisted', 'restart lost writable layer')
    c.d('kill', n)
    require(c.inspect(n)['State']['ExitCode'] == 137, 'kill did not deliver SIGKILL')
    c.d('rm', n)
    c.absent('inspect', n)


@check('container.exit_stdin_wait', 'run|container wait|container inspect')
def exit_stdin_wait(c):
    c.output('sh', '-c', 'exit 37', expected=37)
    require(c.output('cat', opts=('-i',), input='stdin-roundtrip\n') == 'stdin-roundtrip\n', 'stdin differs')
    n = c.container('sh', '-c', 'sleep 1; exit 23', detached=True)
    require(c.d('wait', n).strip() == '23', 'wait returned wrong exit code')
    require(c.inspect(n)['State']['ExitCode'] == 23, 'inspect exit code differs')


@check('container.exec_attach', 'container exec|container attach|container create|container start')
def exec_attach(c):
    n = c.container('cat', opts=('-i',), create=True)
    c.d('start', n)
    require(c.d('exec', '-i', n, 'cat', input='exec-stdin\n') == 'exec-stdin\n', 'exec stdin differs')
    require(c.d('attach', n, input='attach-roundtrip\n', timeout=15) == 'attach-roundtrip\n', 'attach differs')
    require(c.d('wait', n).strip() == '0', 'attached cat did not exit')


@check('container.tty', 'run|container exec')
def tty(c):
    n = c.container('sleep', '300', detached=True)
    for args in [('exec', '-it', n, 'sh', '-c', 'test -t 0 && test -t 1 && read x && printf "tty:%s\\n" "$x"'),
                 ('run', '--name', c.object('container', 'tty'), '--label', c.label, '-it', BASE,
                  'sh', '-c', 'test -t 0 && test -t 1 && read x && printf "tty:%s\\n" "$x"')]:
        master, slave = pty.openpty()
        try:
            proc = c.spawn(*args, stdin=slave, stdout=slave)
            os.close(slave)
            slave = -1
            time.sleep(.3)
            os.write(master, b'roundtrip\n')
            output = b''
            while proc.poll() is None or select.select([master], [], [], 0)[0]:
                if not select.select([master], [], [], c.remaining(15))[0]:
                    raise TimeoutError('TTY response timeout')
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno == 5:
                        break
                    raise
                if not data:
                    break
                output += data
            c.write(output.decode(errors='replace'))
            require(proc.wait(timeout=c.remaining(5)) == 0 and b'tty:roundtrip\r\n' in output, output)
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)


@check('container.rename', 'container rename|container inspect')
def rename(c):
    n = c.container('true', opts=('-e', 'ANSWER=42', '-w', '/tmp'), create=True)
    renamed = c.object('container', 'renamed')
    c.d('rename', n, renamed)
    data = c.inspect(renamed)
    require(data['Name'] == '/' + renamed and data['Config']['WorkingDir'] == '/tmp'
            and 'ANSWER=42' in data['Config']['Env'] and data['Config']['Labels']['hd.check'] == c.id, data['Config'])


@check('container.pause', 'container pause|container unpause|container cp')
def pause(c):
    n = c.container('sh', '-ec', 'i=0; while :; do i=$((i+1)); echo $i >/next; mv /next /value; sleep .05; done', detached=True)
    dest = c.root / 'counter'
    def counter():
        c.d('cp', n + ':/value', dest)
        return int(dest.read_text())
    c.eventually(lambda: c.d('exec', n, 'test', '-s', '/value') == '')
    c.d('pause', n)
    first = counter()
    time.sleep(.3)
    require(counter() == first and c.inspect(n)['State']['Paused'], 'paused process made progress')
    c.d('unpause', n)
    c.eventually(lambda: counter() > first)


@check('container.top_stats', 'container top|container stats')
def top_stats(c):
    n = c.container('sleep', '300', detached=True)
    require('sleep 300' in c.d('top', n), 'top lacks running command')
    row = json.loads(c.d('stats', '--no-stream', '--format', '{{json .}}', n))
    require(int(row['PIDs']) >= 1 and row['Name'] == n, row)


@check('container.logs_follow', 'container logs|container exec')
def logs_follow(c):
    n = c.container('sh', '-ec', 'echo before; while [ ! -f /go ]; do sleep .1; done; echo after', detached=True)
    proc = c.spawn('logs', '--follow', n)
    c.d('exec', n, 'touch', '/go')
    output = proc.communicate(timeout=c.remaining(20))[0].decode()
    c.write(output)
    require(proc.returncode == 0 and output.splitlines() == ['before', 'after'], output)


@check('container.signals', 'container kill|container logs|container wait')
def signals(c):
    n = c.container('sh', '-ec', 'trap "echo signal-received; exit 42" USR1; echo ready; while :; do sleep .1; done', detached=True)
    c.eventually(lambda: 'ready' in c.d('logs', n))
    c.d('kill', '-s', 'USR1', n)
    require(c.d('wait', n).strip() == '42' and 'signal-received' in c.d('logs', n), 'signal trap did not run')


@check('container.cp', 'container cp|container exec')
def copy(c):
    payload = bytes(range(256)) * 1024
    source, dest = c.root / 'payload', c.root / 'returned'
    source.write_bytes(payload)
    n = c.container('sleep', '300', detached=True)
    c.d('cp', source, n + ':/payload')
    digest = hashlib.sha256(payload).hexdigest()
    require(c.d('exec', n, 'sha256sum', '/payload').split()[0] == digest, 'container received wrong bytes')
    c.d('exec', n, 'sh', '-ec', 'cat /payload /payload >/returned')
    c.d('cp', n + ':/returned', dest)
    require(dest.read_bytes() == payload * 2, 'outer received wrong bytes')


@check('image.commit_export', 'container diff|container commit|container export|image history|image import|image inspect|run')
def commit_export(c):
    n = c.container('sleep', '300', detached=True)
    c.d('exec', n, 'sh', '-ec', 'printf committed >/marker')
    require('A /marker' in c.d('diff', n), 'diff missing addition')
    tag = c.object('image', 'commit')
    c.d('commit', '--change', 'LABEL ' + c.label, n, tag)
    ident = c.d('image', 'inspect', '-f', '{{.Id}}', tag).strip()
    require(ident in c.d('history', '--no-trunc', '-q', tag), 'history missing committed image')
    require(c.output('cat', '/marker', image=tag) == 'committed', 'commit lost contents')
    archive = c.root / 'export.tar'
    c.d('export', '-o', archive, n)
    with tarfile.open(archive) as tar:
        require(tar.extractfile('marker').read() == b'committed', 'export lost contents')
    imported = c.object('image', 'import')
    c.d('import', '--change', 'LABEL ' + c.label, archive, imported)
    require(c.output('cat', '/marker', image=imported) == 'committed', 'reimport lost contents')


@check('image.save_load_tag', 'image save|image load|image tag|image rm|image inspect')
def save_load_tag(c):
    tag = c.object('image', 'saved')
    n = c.container('sh', '-ec', 'printf saved >/payload')
    c.d('commit', '--change', 'LABEL ' + c.label, n, tag)
    c.d('rm', n)
    ident = c.d('image', 'inspect', '-f', '{{.Id}}', tag).strip()
    archive = c.root / 'saved.tar'
    c.d('save', '-o', archive, tag)
    require(archive.stat().st_size > 1024 and tarfile.is_tarfile(archive), 'save produced no tar')
    c.d('image', 'rm', tag)
    c.absent('image', 'inspect', ident)
    c.d('load', '-i', archive)
    c.defer('image', 'rm', '-f', tag)
    require(c.d('image', 'inspect', '-f', '{{.Id}}', tag).strip() == ident, 'load changed image identity')
    require(c.output('cat', '/payload', image=tag) == 'saved', 'load lost payload')
    alias = c.object('image', 'alias')
    c.d('tag', tag, alias)
    require(c.d('image', 'inspect', '-f', '{{.Id}}', alias).strip() == ident, 'tag changed identity')
    c.d('rmi', alias)
    c.absent('image', 'inspect', alias)


@check('image.buildkit', 'image build|buildx build|run', timeout=240)
def buildkit(c):
    folder = c.root / 'context'
    folder.mkdir()
    shutil.copy2(FIXTURES / 'build.Dockerfile', folder / 'Dockerfile')
    tag = c.object('image', 'built')
    args = ['--pull=false', '--progress=plain', '--label', c.label, '--build-arg', 'CACHE_ID=' + c.name,
            '--build-arg', 'MARKER=' + c.name, '-t', tag, str(folder)]
    c.d('build', *args, timeout=90)
    first = c.output('cat', '/artifact', '/count', '/nonce', image=tag).splitlines()
    require(first[0] == c.name and first[1] == '1' and len(first[2]) == 36, first)
    layers = c.d('image', 'inspect', '-f', '{{json .RootFS.Layers}}', tag).strip()
    c.d('build', *args, timeout=90)
    require(c.d('image', 'inspect', '-f', '{{json .RootFS.Layers}}', tag).strip() == layers, 'warm build changed filesystem layers')
    require(c.output('cat', '/artifact', '/count', '/nonce', image=tag).splitlines() == first, 'cached content changed')
    changed = ['MARKER=' + c.name + '-changed' if arg == 'MARKER=' + c.name else arg for arg in args]
    c.d('build', *changed, timeout=90)
    reused = c.output('cat', '/artifact', '/count', '/nonce', image=tag).splitlines()
    require(reused[:2] == [c.name + '-changed', '2'] and reused[2] != first[2], 'cache mount did not persist: ' + str(reused))
    c.d('build', '--no-cache', *args, timeout=90)
    second = c.output('cat', '/artifact', '/count', '/nonce', image=tag).splitlines()
    require(second[:2] == [c.name, '1'] and second[2] not in (first[2], reused[2]), 'no-cache did not rebuild: ' + str(second))
    c.d('buildx', 'build', '--load', '--no-cache', *args, timeout=90)
    third = c.output('cat', '/artifact', '/count', '/nonce', image=tag).splitlines()
    require(third[:2] == [c.name, '1'] and third[2] != second[2], 'buildx result differs: ' + str(third))


def pinned(key):
    item = build.LOCK[key]
    require(re.fullmatch(r'.+@sha256:[0-9a-f]{64}', item['reference'])
            and item['reference'].endswith(item['sha256']), 'image pin missing or inconsistent')
    return item['reference']


@check('image.pull', 'image pull|image inspect|run', timeout=150)
def pull(c):
    ref = pinned('functional_pull_image')
    c.defer('image', 'rm', '-f', ref)
    c.d('pull', '--platform', 'linux/amd64', ref, timeout=120)
    require(ref in json.loads(c.d('image', 'inspect', '-f', '{{json .RepoDigests}}', ref)), 'pull digest differs')
    require(c.output('cat', '/etc/alpine-release', image=ref).strip() == '3.24.2', 'pulled image contents differ')


@check('image.registry_roundtrip', 'image push|image pull|image tag|run', timeout=180)
def registry_roundtrip(c):
    ref = pinned('functional_registry_image')
    c.defer('image', 'rm', '-f', ref)
    try:
        c.d('pull', '--platform', 'linux/amd64', ref, timeout=90)
    except (AssertionError, RuntimeError) as error:
        raise NotTested('pinned public ECR registry image could not be pulled: ' + str(error)) from error
    port = free_port()
    vol = c.volume('registry')
    registry = c.container(opts=('--network', 'host', '-e', f'REGISTRY_HTTP_ADDR=127.0.0.1:{port}',
                                 '-v', vol + ':/var/lib/registry'), image=ref, detached=True)
    c.eventually(lambda: '{}' in c.output('wget', '-qO-', '-T', '2', f'http://127.0.0.1:{port}/v2/', opts=('--network', 'host')))
    producer = c.container('sh', '-ec', 'printf registry-roundtrip >/marker')
    tag = f'127.0.0.1:{port}/hd/{c.name}:roundtrip'
    c.defer('image', 'rm', '-f', tag)
    c.d('commit', '--change', 'LABEL ' + c.label, producer, tag)
    ident = c.d('image', 'inspect', '-f', '{{.Id}}', tag).strip()
    pushed = c.d('push', tag, timeout=60)
    digest = re.search(r'digest: (sha256:[0-9a-f]{64})', pushed)
    require(digest, 'push returned no manifest digest')
    exact = tag.rsplit(':', 1)[0] + '@' + digest.group(1)
    c.defer('image', 'rm', '-f', exact)
    c.d('rm', producer)
    c.d('image', 'rm', tag)
    c.absent('image', 'inspect', ident)
    c.d('pull', exact, timeout=60)
    require(c.d('image', 'inspect', '-f', '{{.Id}}', exact).strip() == ident, 'registry changed config identity')
    require(c.output('cat', '/marker', image=exact) == 'registry-roundtrip', 'registry data round trip failed')
    require(c.inspect(registry)['State']['Running'], 'registry exited')


@check('volume.shared', 'volume create|volume ls|volume inspect|volume rm|run')
def volume_shared(c):
    v = c.volume()
    require(v in c.d('volume', 'ls', '-q', '--filter', 'label=' + c.label).splitlines(), 'volume not listed')
    require(json.loads(c.d('volume', 'inspect', v))[0]['Labels']['hd.check'] == c.id, 'volume label lost')
    a = c.container('sh', '-ec', 'printf volume-payload >/data/file; exec sleep 300', opts=('-v', v + ':/data'), detached=True)
    b = c.container('sleep', '300', opts=('-v', v + ':/data'), detached=True)
    c.eventually(lambda: c.d('exec', b, 'cat', '/data/file') == 'volume-payload')
    c.d('exec', b, 'sh', '-ec', 'printf updated >/data/file')
    require(c.d('exec', a, 'cat', '/data/file') == 'updated', 'shared volume update missing')
    c.d('rm', '-f', a, b)
    require(c.shell('cat /data/file', opts=('-v', v + ':/data')) == 'updated', 'volume lost data after recreation')
    c.d('volume', 'rm', v)
    c.absent('volume', 'inspect', v)


@check('storage.tmpfs', 'run|container exec|container restart')
def tmpfs(c):
    n = c.container('sleep', '300', opts=('--tmpfs', '/scratch:rw,size=8m,mode=1777'), detached=True)
    c.d('exec', n, 'sh', '-ec', 'grep -q " /scratch tmpfs " /proc/mounts; printf tmpfs >/scratch/data; test "$(cat /scratch/data)" = tmpfs')
    c.d('restart', '-t', '1', n)
    c.d('exec', n, 'sh', '-ec', 'test ! -e /scratch/data; grep -q " /scratch tmpfs " /proc/mounts')


@check('storage.bind_metadata', 'run|container exec', timeout=90)
def bind_metadata(c):
    share = c.ctx.share / c.id
    share.mkdir(parents=True)
    source = share / 'from-outer'
    source.write_bytes(b'outer')
    os.utime(source, (1700000000, 1700000000))
    n = c.container('sleep', '300', opts=('--mount', f'type=bind,src={share},dst=/share'), detached=True)
    require(c.d('exec', n, 'cat', '/share/from-outer') == 'outer', 'outer write invisible')
    require(c.d('exec', n, 'stat', '-c', '%s %Y', '/share/from-outer').strip() == '5 1700000000', 'metadata differs')
    source.write_bytes(b'outer-changed')
    os.utime(source, (1700000050, 1700000050))
    require(c.d('exec', n, 'stat', '-c', '%s %Y', '/share/from-outer').strip() == '13 1700000050', 'metadata cache stale')
    c.d('exec', n, 'sh', '-ec', 'mv /share/from-outer /share/renamed; printf inner >/share/from-inner; touch -m -t 202001020304.05 /share/from-inner')
    require(not source.exists() and (share / 'renamed').read_bytes() == b'outer-changed', 'rename invisible')
    require((share / 'from-inner').read_bytes() == b'inner' and (share / 'from-inner').stat().st_mtime < 1600000000, 'inner metadata invisible')
    (share / 'renamed').unlink()
    c.d('exec', n, 'test', '!', '-e', '/share/renamed')
    c.d('exec', n, 'rm', '/share/from-inner')
    require(not (share / 'from-inner').exists(), 'inner unlink invisible')


@check('security.readonly_user', 'run')
def readonly_user(c):
    c.shell('test "$(id -u)" = 1000; test "$(id -g)" = 1001; touch /tmp/owned; test "$(stat -c %u:%g /tmp/owned)" = 1000:1001',
            opts=('--user', '1000:1001'))
    c.shell('test "$(id -u)" = 0; if touch /forbidden 2>/tmp/error; then exit 1; fi; grep -qi "read-only" /tmp/error',
            opts=('--read-only', '--tmpfs', '/tmp'))


@check('security.seccomp', 'run', helpers=True, timeout=180)
def seccomp(c):
    c.shell('grep -Eq "Seccomp:[[:space:]]*2" /proc/self/status; /probe seccomp', image=HELPERS)


@check('security.privileged_mount', 'run')
def privileged_mount(c):
    c.shell('mount -t tmpfs none /mnt; grep -q " /mnt tmpfs " /proc/mounts; echo mounted >/mnt/file; test "$(cat /mnt/file)" = mounted; umount /mnt', opts=('--privileged',))


@check('security.net_admin', 'run')
def net_admin(c):
    c.shell('ip link add hdtest type dummy; ip link set hdtest up; ip link show hdtest | grep -q UP; ip link del hdtest; ! ip link show hdtest', opts=('--cap-add', 'NET_ADMIN'))


@check('security.user_namespace', 'run')
def user_namespace(c):
    c.shell('before=$(readlink /proc/self/ns/user); export before; unshare -Ur sh -ec \'test "$(id -u)" = 0; test "$(readlink /proc/self/ns/user)" != "$before"; cat /proc/self/uid_map; touch /tmp/userns\'; test -f /tmp/userns', opts=('--privileged',))


@check('resource.memory_oom', 'run|container inspect', helpers=True, timeout=180)
def memory_oom(c):
    name = c.object('container', 'oom')
    c.d('run', '--name', name, '--label', c.label, '--memory', '32m', '--memory-swap', '32m', HELPERS, '/probe', 'oom', expected=137)
    state = c.inspect(name)['State']
    require(state['OOMKilled'] and state['ExitCode'] == 137, state)


@check('resource.pids', 'run', helpers=True, timeout=180)
def pids(c):
    row = json.loads(c.output('/probe', 'pids', opts=('--pids-limit', '12'), image=HELPERS))
    require(row['limited'] and 1 <= row['children'] <= 11 and row['events'] > 0, row)


def cpu_result(text):
    row = json.loads(text)
    require(row['wall'] >= 2.5 and row['cpu'] > 0 and row['iterations'] > 0, row)
    return row


def quota_effect(reference, limited, quota=.25):
    rate = limited['cpu'] / limited['wall']
    require(rate <= quota + .08, f'CPU quota exceeded: {rate:.3f} cores (requested {quota})')
    require(limited['throttled'] > 0, 'no cgroup throttling despite busy loop')
    if reference['cpu'] / reference['wall'] < .70 or rate < quota * .70:
        raise NotTested('host contention prevents a conclusive CPU quota measurement')
    require(rate < (reference['cpu'] / reference['wall']) * .65, 'quota has no measured effect')


@check('resource.cpu_quota', 'run', helpers=True, timeout=180)
def cpu_quota(c):
    reference = cpu_result(c.output('/probe', 'cpu', '3', image=HELPERS))
    limited = cpu_result(c.output('/probe', 'cpu', '4', image=HELPERS, opts=('--cpus', '.25')))
    quota_effect(reference, limited)
    explicit = cpu_result(c.output('/probe', 'cpu', '4', image=HELPERS, opts=('--cpu-period', '50000', '--cpu-quota', '12500')))
    quota_effect(reference, explicit)


@check('resource.cpuset', 'run', helpers=True, timeout=180)
def cpuset(c):
    for cpus in ('0', '1', '0-1'):
        row = cpu_result(c.output('/probe', 'cpu', '3', cpus, opts=('--cpuset-cpus', cpus), image=HELPERS))
        expected = [0, 1] if cpus == '0-1' else [int(cpus)]
        require(row['allowed'] == expected and row['observed'] and set(row['observed']) <= set(expected), row)


@check('resource.update', 'container update|container exec', helpers=True, timeout=180)
def update(c):
    n = c.container('sleep', '300', opts=('--memory', '64m', '--memory-swap', '64m', '--pids-limit', '64'), image=HELPERS, detached=True)
    ident = c.inspect(n)['Id']
    reference = cpu_result(c.d('exec', n, '/probe', 'cpu', '3'))
    c.d('update', '--memory', '96m', '--memory-swap', '96m', '--pids-limit', '48', '--cpus', '.25', '--cpuset-cpus', '0', n)
    c.d('exec', n, 'sh', '-ec', 'test "$(cat /sys/fs/cgroup/memory.max)" = 100663296; test "$(cat /sys/fs/cgroup/pids.max)" = 48; test "$(cat /sys/fs/cgroup/cpu.max)" = "25000 100000"')
    limited = cpu_result(c.d('exec', n, '/probe', 'cpu', '4', '0'))
    require(limited['allowed'] == [0] and limited['observed'] == [0], limited)
    quota_effect(reference, limited)
    c.d('update', '--cpus', '1', n)
    recovered = cpu_result(c.d('exec', n, '/probe', 'cpu', '3', '0'))
    require(recovered['cpu'] / recovered['wall'] > .65, 'CPU delivery did not recover after update')
    require(c.inspect(n)['Id'] == ident, 'update replaced the container')


def http_server(c, network=None, opts=(), suffix=None):
    return c.container('sh', '-ec', 'printf "HTTP/1.0 200 OK\\r\\nContent-Length: 12\\r\\n\\r\\nhttp-payload" >/response; exec nc -lk -p 8080 -e cat /response',
                       opts=(*opts, *(['--network', network] if network else [])), detached=True, suffix=suffix)


@check('network.bridge_dns', 'network create|network ls|network inspect|network connect|network disconnect|network rm|run')
def bridge_dns(c):
    net = c.network()
    require(net in c.d('network', 'ls', '--format', '{{.Name}}', '--filter', 'label=' + c.label).splitlines(), 'network absent')
    require(json.loads(c.d('network', 'inspect', net))[0]['Labels']['hd.check'] == c.id, 'network label lost')
    server = http_server(c)
    c.d('network', 'connect', '--alias', 'peer', net, server)
    ip = c.inspect(server)['NetworkSettings']['Networks'][net]['IPAddress']
    client = c.container('sleep', '300', opts=('--network', net), detached=True)
    require(ip in c.d('exec', client, 'nslookup', 'peer'), 'bridge DNS resolved wrong address')
    c.eventually(lambda: c.d('exec', client, 'wget', '-qO-', '-T', '2', 'http://peer:8080/') == 'http-payload')
    c.d('network', 'disconnect', net, server)
    require(net not in c.inspect(server)['NetworkSettings']['Networks'], 'network disconnect ignored')
    c.d('exec', client, 'wget', '-qO-', '-T', '2', f'http://{ip}:8080/', expected='failure')
    c.d('rm', '-f', server, client)
    c.d('network', 'rm', net)
    c.absent('network', 'inspect', net)


@check('network.ipv6', 'network create|run')
def ipv6(c):
    net = c.network('--ipv6', '--subnet', 'fd42:4844:100::/64')
    peer = c.container('sleep', '300', opts=('--network', net), detached=True)
    ip = c.inspect(peer)['NetworkSettings']['Networks'][net]['GlobalIPv6Address']
    require(ip.startswith('fd42:4844:100:'), 'missing IPv6 address')
    c.output('ping', '-6', '-c', '2', '-W', '3', ip, opts=('--network', net))


def free_port(udp=False, host='127.0.0.1'):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def echo(c, port, udp=False, host='127.0.0.1'):
    payload = b'harddind-roundtrip\x00\xff' * (32 if udp else 4096)
    def exchange():
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
            sock.settimeout(c.remaining(2))
            sock.connect((host, port))
            sock.sendall(payload)
            data = b''
            while len(data) < len(payload):
                part = sock.recv(len(payload))
                if not part:
                    break
                data += part
                if udp:
                    break
            require(data == payload, f'{host}:{port} data mismatch: {len(data)} bytes')
            return True
    c.eventually(exchange, timeout=15)
    c.write(f'verified {len(payload)} bytes via {host}:{port}/{"udp" if udp else "tcp"}')


def released(c, port, udp=False, host='127.0.0.1'):
    def bind():
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
            if not udp:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return True
    c.eventually(bind, timeout=10)


def published(c, name, udp=False, host='127.0.0.1'):
    proto = 'udp' if udp else 'tcp'
    bindings = c.inspect(name)['NetworkSettings']['Ports']['7777/' + proto]
    row = next(row for row in bindings if row['HostIp'] == host)
    port = int(row['HostPort'])
    require(0 < port < 65536, row)
    require(str(port) in c.d('port', name, '7777/' + proto), 'port command disagrees')
    return port


def echo_server(c, publish, udp=False, opts=(), suffix=None):
    proto = 'udp' if udp else 'tcp'
    return c.container('/network_probe', proto, '7777', '0.0.0.0', image=HELPERS, detached=True,
                       opts=('-p', publish, *opts), suffix=suffix)


@check('network.publish_tcp_udp', 'run|container port|container stop|container start|container rm', helpers=True, timeout=180)
def publish_tcp_udp(c):
    for udp in (False, True):
        port = free_port(udp)
        suffix = '/udp' if udp else '/tcp'
        name = echo_server(c, f'127.0.0.1:{port}:7777{suffix}', udp)
        require(published(c, name, udp) == port, 'wrong fixed port')
        echo(c, port, udp)
        c.d('stop', '-t', '1', name)
        released(c, port, udp)
        c.d('start', name)
        echo(c, port, udp)
        c.d('rm', '-f', name)
        released(c, port, udp)


@check('network.publish_conflicts', 'container create|container start|network create|container inspect', helpers=True, timeout=180)
def publish_conflicts(c):
    net = c.network('--opt', 'com.docker.network.bridge.host_binding_ipv4=127.0.0.1')
    for network in ('bridge', net):
        first = free_port()
        with socket.socket() as holder:
            holder.bind(('127.0.0.1', 0)); holder.listen(1)
            occupied = holder.getsockname()[1]
            n = c.container('/network_probe', 'tcp', '7777', '0.0.0.0', create=True, image=HELPERS,
                            opts=('--network', network, *(['--network-alias', 'retained'] if network != 'bridge' else []),
                                  '-p', f'127.0.0.1:{first}:7777', '-p', f'127.0.0.1:{occupied}:8888'))
            c.d('start', n, expected='failure')
            require(not c.inspect(n)['State']['Running'], 'conflicted container is running')
            if network != 'bridge':
                require('retained' in c.inspect(n)['NetworkSettings']['Networks'][net]['Aliases'], 'rollback lost network alias')
            released(c, first)
        c.d('start', n)
        echo(c, first)
        c.d('rm', '-f', n)
        released(c, first); released(c, occupied)


@check('network.publish_addresses', 'run|container port|container rm', helpers=True, timeout=180)
def publish_addresses(c):
    for host in ('127.0.0.2', '::1'):
        for udp in (False, True):
            port = free_port(udp, host)
            addr = f'[{host}]' if ':' in host else host
            n = echo_server(c, f'{addr}:{port}:7777/{"udp" if udp else "tcp"}', udp)
            require(published(c, n, udp, host) == port, 'wrong host binding')
            echo(c, port, udp, host)
            c.d('rm', '-f', n)
            released(c, port, udp, host)
    for udp in (False, True):
        port = free_port(udp)
        one = echo_server(c, f'127.0.0.1:{port}:7777/{"udp" if udp else "tcp"}', udp)
        two = echo_server(c, f'127.0.0.2:{port}:7777/{"udp" if udp else "tcp"}', udp)
        echo(c, port, udp); echo(c, port, udp, '127.0.0.2')
        c.d('rm', '-f', two)
        released(c, port, udp, '127.0.0.2'); echo(c, port, udp)
        c.d('rm', '-f', one)


@check('network.publish_wildcard', 'container create|container start|run', helpers=True, timeout=180)
def publish_wildcard(c):
    for specific, wildcard in [('127.0.0.1', '0.0.0.0'), ('::1', '::')]:
        for reverse in (False, True):
            port = free_port(host=specific)
            hosts = [wildcard, specific] if reverse else [specific, wildcard]
            def spec(host):
                return f'[{host}]:{port}:7777' if ':' in host else f'{host}:{port}:7777'
            active = echo_server(c, spec(hosts[0]))
            failed = c.container('/network_probe', 'tcp', '7777', '0.0.0.0', image=HELPERS,
                                 create=True, opts=('-p', spec(hosts[1])))
            c.d('start', failed, expected='failure')
            require(not c.inspect(failed)['State']['Running'], 'wildcard conflict not rejected')
            echo(c, port, host=specific)
            c.d('rm', '-f', failed, active)
            released(c, port, host=specific)


@check('network.publish_dynamic', 'run|container port', helpers=True, timeout=180)
def publish_dynamic(c):
    n = echo_server(c, '127.0.0.1::7777')
    echo(c, published(c, n))
    lower = min(free_port(), 65530)
    n = echo_server(c, f'127.0.0.1:{lower}-{lower+2}:7777')
    port = published(c, n)
    require(lower <= port <= lower + 2, 'port range ignored')
    echo(c, port)
    net = c.network('--opt', 'com.docker.network.bridge.host_binding_ipv4=127.0.0.1')
    n = c.container('/network_probe', 'tcp', '7777', '0.0.0.0', opts=('--network', net, '--expose', '7777', '-P'), image=HELPERS, detached=True)
    echo(c, published(c, n))
    processes, names = [], []
    for i in range(4):
        name = c.object('container', 'parallel-' + str(i)); names.append(name)
        processes.append(c.spawn('run', '-d', '--name', name, '--label', c.label,
                                 '-p', '127.0.0.1::7777', HELPERS, '/network_probe', 'tcp', '7777', '0.0.0.0'))
    for proc in processes:
        out = proc.communicate(timeout=c.remaining(30))[0]
        c.write(out.decode())
        require(proc.returncode == 0, 'concurrent publish failed')
    ports = [published(c, name) for name in names]
    require(len(set(ports)) == 4, 'concurrent publish reused port')
    for port in ports:
        echo(c, port)


@check('network.egress_https_dns', 'run', timeout=90)
def egress(c):
    item = build.LOCK['functional_https']
    host = item['url'].split('/')[2]
    # One retry: the outer internet path is shared and occasionally stalls.
    c.output('sh', '-ec', 'nslookup "$1"; wget -T 20 -qO /tmp/download "$2" || wget -T 20 -qO /tmp/download "$2"; '
             'echo "$3  /tmp/download" | sha256sum -c -', 'sh', host, item['url'], item['sha256'])


@check('network.egress_local_http_tls', 'run', timeout=180)
def local_egress(c):
    import importlib.util
    spec = importlib.util.spec_from_file_location('local_egress', config.REPO / 'tests/publish/local_egress.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.run(c)


@check('system.context', 'context create|context ls|context inspect|context rm|system info', base=False)
def context(c):
    _, env = c.ctx.docker_cmd()
    name = c.object('context')
    ident = c.d('info', '--format', '{{.ID}}').strip()
    c.d('context', 'create', '--description', c.label, '--docker', 'host=' + env['DOCKER_HOST'], name)
    require(name in c.d('context', 'ls', '--format', '{{.Name}}').splitlines(), 'context absent')
    row = json.loads(c.d('context', 'inspect', name))[0]
    require(row['Endpoints']['docker']['Host'] == env['DOCKER_HOST'], row)
    require(c.d('--context', name, 'info', '--format', '{{.ID}}').strip() == ident, 'context targets wrong daemon')
    c.d('context', 'rm', name)
    c.absent('context', 'inspect', name)


@check('system.events', 'system events|container create|container start|container wait|container rm')
def events(c):
    since = str(int(time.time()) - 1)
    n = c.container('true', create=True)
    ident = c.inspect(n)['Id']
    c.d('start', n); c.d('wait', n); c.d('rm', n)
    output = c.d('events', '--since', since, '--until', str(int(time.time()) + 1),
                 '--filter', 'label=' + c.label, '--format', '{{json .}}')
    rows = [json.loads(line) for line in output.splitlines()]
    actions = {row['Action'] for row in rows if row.get('Actor', {}).get('ID') == ident}
    require({'create', 'start', 'die', 'destroy'} <= actions, rows)


@check('system.df', 'system df')
def disk_usage(c):
    n = c.container('sh', '-ec', 'dd if=/dev/zero of=/payload bs=1M count=2')
    rows = [json.loads(line) for line in c.d('system', 'df', '--format', '{{json .}}').splitlines()]
    types = {row['Type']: row for row in rows}
    require('Images' in types and 'Containers' in types and int(types['Containers']['TotalCount']) >= 1, rows)
    require(c.inspect(n)['State']['ExitCode'] == 0, 'data write failed')


@check('system.prune', 'container prune|image prune|volume prune|network prune|system prune', timeout=120)
def prune(c):
    for system in (False, True):
        suffix = 'system' if system else 'individual'
        vol = c.volume(suffix)
        net = c.network(suffix=suffix)
        image = c.object('image', suffix)
        n = c.container('sh', '-ec', 'printf prune >/prune')
        c.d('commit', '--change', 'LABEL ' + c.label, n, image)
        if system:
            c.d('system', 'prune', '-af', '--filter', 'label=' + c.label)
            c.d('volume', 'prune', '-af', '--filter', 'label=' + c.label)
        else:
            c.d('container', 'prune', '-f', '--filter', 'label=' + c.label)
            c.d('image', 'prune', '-af', '--filter', 'label=' + c.label)
            c.d('network', 'prune', '-f', '--filter', 'label=' + c.label)
            c.d('volume', 'prune', '-af', '--filter', 'label=' + c.label)
        for args in [('inspect', n), ('image', 'inspect', image), ('network', 'inspect', net), ('volume', 'inspect', vol)]:
            c.absent(*args)
        c.d('image', 'inspect', BASE)


@check('container.restart_health', 'run|container inspect|container logs', timeout=90)
def restart_health(c):
    n = c.container('sh', '-ec', 'sleep 1; echo failing; exit 7', opts=('--restart', 'on-failure:2'), detached=True)
    c.eventually(lambda: c.inspect(n)['RestartCount'] == 2 and not c.inspect(n)['State']['Running'], timeout=20)
    require(c.d('logs', n).splitlines().count('failing') == 3, 'restart count disagrees with process executions')
    h = c.container('sh', '-ec', 'touch /ready; exec sleep 300', opts=('--health-cmd', 'test -f /ready',
                    '--health-interval', '1s', '--health-timeout', '1s', '--health-retries', '2'), detached=True)
    c.eventually(lambda: c.inspect(h)['State']['Health']['Status'] == 'healthy')
    c.d('exec', h, 'rm', '/ready')
    c.eventually(lambda: c.inspect(h)['State']['Health']['Status'] == 'unhealthy')


def semantic(c, program, args=(), marker=None):
    output = c.output('/' + program, *args, image=HELPERS)
    if marker:
        require(marker in output, 'semantic program did not report completion')
    else:
        row = json.loads(output)
        require(row['test'] == args[0] and row['seconds'] >= 0, row)
        if program == 'dispatchbench':
            require(row['verified'] is True, row)
        else:
            require(row.get('verified') is True, 'core computation did not verify its result')


check('process.threads', 'run', helpers=True, timeout=180)(
    lambda c: semantic(c, 'thread_semantics', marker='THREAD_SEMANTICS_END status=0'))
check('process.mapping', 'run', helpers=True, timeout=180)(
    lambda c: semantic(c, 'mapping_semantics', marker='MAPPING_SEMANTICS_END status=0'))
for _mode in ('identity', 'fdio', 'mapping', 'thread_syscalls', 'isolation', 'parallel_exec'):
    check('process.dispatch_' + _mode, 'run', helpers=True, timeout=180)(
        lambda c, mode=_mode: semantic(c, 'dispatchbench', (mode,)))
for _mode in ('threads', 'processes', 'signals', 'exec', 'syscall'):
    check('process.core_' + _mode, 'run', helpers=True, timeout=180)(
        lambda c, mode=_mode: semantic(c, 'corebench', (mode, '4')))


def parse_time(value):
    return datetime.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', value.replace('Z', '+00:00'))).timestamp()


@check('compose.workflow', 'compose config|compose build|compose up|compose down|compose exec|compose logs|compose ps|compose scale|compose run', timeout=240)
def compose(c):
    fixture = json.loads((FIXTURES / 'compose/compose.yaml').read_text())
    image = c.object('image', 'built')
    for name, spec in fixture['services'].items():
        spec['labels'] = {'hd.check': c.id}
        if name == 'builder':
            spec['image'] = image
            spec['build']['context'] = str(FIXTURES / 'compose/builder')
            spec['build']['labels'] = {'hd.check': c.id}
    fixture['volumes']['data']['labels'] = {'hd.check': c.id}
    fixture['networks']['default']['labels'] = {'hd.check': c.id}
    path = c.root / 'compose.json'
    path.write_text(json.dumps(fixture))
    prefix = ('compose', '-p', c.name, '-f', path)
    c.defer(*prefix, 'down', '--volumes', '--remove-orphans', '--timeout', '1')
    def comp(*args):
        return c.d(*prefix, *args, timeout=90)
    cfg = json.loads(comp('config', '--format', 'json'))
    require(cfg['services']['web']['depends_on']['db']['condition'] == 'service_healthy', 'depends_on lost')
    comp('build', '--no-cache', 'builder')
    require(comp('run', '--rm', '-T', 'builder').strip() == 'compose-built', 'compose build lost data')
    since = str(int(time.time()) - 1)
    comp('up', '-d', '--pull', 'never', 'db', 'web', 'worker')
    ids = {name: comp('ps', '-q', name).strip() for name in ('db', 'web', 'worker')}
    require(all(ids.values()), ids)
    db, web = c.inspect(ids['db']), c.inspect(ids['web'])
    require(db['State']['Health']['Status'] == 'healthy', db['State'])
    raw = c.d('events', '--since', since, '--until', str(int(time.time()) + 1),
              '--filter', 'container=' + ids['db'], '--filter', 'event=health_status', '--format', '{{json .}}')
    healthy = [json.loads(line) for line in raw.splitlines() if json.loads(line)['Action'] == 'health_status: healthy']
    require(healthy and parse_time(web['State']['StartedAt']) >= min(e['timeNano'] for e in healthy) / 1e9 - .000001,
            'web started before the healthy transition')
    require(comp('exec', '-T', 'web', 'wget', '-qO-', '-T', '3', 'http://db:8080/') == 'db-payload', 'service DNS/HTTP failed')
    comp('exec', '-T', 'db', 'sh', '-ec', 'printf persistent >/data/value')
    comp('up', '-d', '--force-recreate', '--no-deps', 'db')
    require(comp('exec', '-T', 'db', 'cat', '/data/value') == 'persistent', 'compose recreation lost volume')
    comp('scale', 'worker=3')
    workers = comp('ps', '-q', 'worker').split()
    require(len(workers) == 3, workers)
    addresses = []
    for worker in workers:
        info = c.inspect(worker)
        ip = next(iter(info['NetworkSettings']['Networks'].values()))['IPAddress']
        addresses.append(ip)
        require(comp('exec', '-T', 'web', 'wget', '-qO-', '-T', '3', f'http://{ip}:8080/').strip() == info['Config']['Hostname'], 'scaled replica serves wrong identity')
    dns = comp('exec', '-T', 'web', 'nslookup', 'worker')
    require(all(ip in dns for ip in addresses), 'scaled DNS missing replicas')
    comp('scale', 'worker=1')
    require(len(comp('ps', '-q', 'worker').split()) == 1, 'scale down failed')
    require('web-started' in comp('logs', '--no-color', 'web'), 'compose logs missing marker')
    comp('down', '--volumes', '--remove-orphans', '--timeout', '1')
    require(not c.d('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + c.name).strip(), 'compose containers remain')
    for kind in ('network', 'volume'):
        require(not c.d(kind, 'ls', '-q', '--filter', 'label=' + c.label).strip(), kind + ' remains after down')


@check('swarm.workflow', 'swarm init|swarm leave|node ls|service create|service inspect|service ls|service ps|service logs|service scale|service rm|secret create|secret inspect|secret rm|config create|config inspect|config rm|network create', timeout=240)
def swarm(c):
    c.defer('swarm', 'leave', '--force')
    ip = c.output('sh', '-ec', 'ip -4 -o addr show vec0 | awk \'{print $4}\' | cut -d/ -f1 | head -n 1', opts=('--network', 'host')).strip()
    require(re.fullmatch(r'\d+\.\d+\.\d+\.\d+', ip), 'guest uplink IPv4 missing')
    c.d('swarm', 'init', '--advertise-addr', ip, '--listen-addr', ip + ':2377')
    require(c.d('info', '-f', '{{.Swarm.LocalNodeState}}').strip() == 'active', 'swarm not active')
    require('Ready' in c.d('node', 'ls', '--format', '{{.Status}}'), 'manager not Ready')
    net = c.network('--driver', 'overlay', '--attachable')
    secret, conf = c.object('secret'), c.object('config')
    c.d('secret', 'create', '--label', c.label, secret, '-', input='secret-payload')
    c.d('config', 'create', '--label', c.label, conf, '-', input='config-payload')
    for kind, name in [('secret', secret), ('config', conf)]:
        require(json.loads(c.d(kind, 'inspect', name))[0]['Spec']['Labels']['hd.check'] == c.id, kind + ' label lost')
    service = c.object('service')
    image_id = c.d('image', 'inspect', '-f', '{{.Id}}', BASE).strip()
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image_id), 'base image ID missing')
    script = 'test "$(cat /run/secrets/secret)" = secret-payload; test "$(cat /cfg)" = config-payload; printf "HTTP/1.0 200 OK\\r\\nContent-Length: 11\\r\\n\\r\\nvip-payload" >/response; echo service-ready; exec nc -lk -p 8080 -e cat /response'
    c.d('service', 'create', '--detach', '--name', service, '--label', c.label, '--container-label', c.label,
        '--network', net, '--no-resolve-image', '--replicas', '2', '--secret', f'source={secret},target=secret',
        '--config', f'source={conf},target=/cfg', image_id, 'sh', '-ec', script)
    try:
        c.eventually(lambda: c.d('service', 'ls', '--filter', 'name=' + service, '--format', '{{.Replicas}}').strip() == '2/2', timeout=45)
    except AssertionError as error:
        tasks = c.d('service', 'ps', '--no-trunc', service)
        c.d('service', 'logs', service)
        raise AssertionError(str(error) + '\n' + tasks[-650:]) from error
    tasks = c.d('ps', '-q', '--filter', 'label=com.docker.swarm.service.name=' + service).split()
    require(len(tasks) == 2, 'service tasks absent')
    for task in tasks:
        require(c.d('exec', task, 'cat', '/run/secrets/secret', '/cfg') == 'secret-payloadconfig-payload', 'secret/config contents wrong')
    require('Running' in c.d('service', 'ps', service, '--format', '{{.CurrentState}}'), 'service ps missing running tasks')
    require('service-ready' in c.d('service', 'logs', service), 'service logs missing')
    endpoint = json.loads(c.d('service', 'inspect', service))[0]['Endpoint']
    network_id = json.loads(c.d('network', 'inspect', net))[0]['Id']
    vip = next(v['Addr'].split('/')[0] for v in endpoint['VirtualIPs'] if v['NetworkID'] == network_id)
    require(c.output('wget', '-qO-', '-T', '5', f'http://{vip}:8080/', opts=('--network', net)) == 'vip-payload', 'direct overlay VIP request failed')
    require(c.output('wget', '-qO-', '-T', '5', f'http://{service}:8080/', opts=('--network', net)) == 'vip-payload', 'overlay VIP HTTP failed')
    c.d('service', 'scale', '--detach', service + '=1')
    c.eventually(lambda: c.d('service', 'ls', '--filter', 'name=' + service, '--format', '{{.Replicas}}').strip() == '1/1', timeout=30)
    c.d('service', 'rm', service)
    c.eventually(lambda: not c.d('ps', '-q', '--filter', 'label=com.docker.swarm.service.name=' + service).strip())
    c.d('secret', 'rm', secret); c.d('config', 'rm', conf)
    c.d('network', 'rm', net)
    c.d('swarm', 'leave', '--force')
    require(c.d('info', '-f', '{{.Swarm.LocalNodeState}}').strip() == 'inactive', 'swarm leave ignored')


@check('network.publish_alias_lifetime', 'run|container create|container start|container rm', helpers=True, timeout=180)
def publish_alias_lifetime(c):
    def addresses():
        raw = c.output('/network_probe', 'addresses', 'vec0', image=HELPERS, opts=('--network', 'host'))
        return {line.split()[1] for line in raw.splitlines()}
    baseline = addresses()
    require('10.0.2.15' in baseline, 'guest uplink is missing')
    previous = None
    for _ in range(2):
        tcp, udp = free_port(host='127.0.0.2'), free_port(True, '127.0.0.2')
        one = echo_server(c, f'127.0.0.2:{tcp}:7777')
        two = echo_server(c, f'127.0.0.2:{udp}:7777/udp', True)
        echo(c, tcp, host='127.0.0.2'); echo(c, udp, True, '127.0.0.2')
        live = addresses() - baseline
        require(len(live) == 1 and (previous is None or previous == live), 'alias allocation/reuse differs')
        c.d('rm', '-f', one)
        require(addresses() == baseline | live, 'TCP removal destroyed the UDP alias')
        echo(c, udp, True, '127.0.0.2')
        c.d('rm', '-f', two)
        require(addresses() == baseline, 'unused address alias leaked')
        previous = live
    with socket.socket() as holder:
        holder.bind(('127.0.0.3', 0)); holder.listen(1)
        port = holder.getsockname()[1]
        n = c.container('/network_probe', 'tcp', '7777', '0.0.0.0', image=HELPERS,
                        create=True, opts=('-p', f'127.0.0.3:{port}:7777'))
        c.d('start', n, expected='failure')
        require(not c.inspect(n)['State']['Running'] and addresses() == baseline, 'failed start leaked alias')


@check('network.manual_forward_ownership', 'container create|container start|container rm', helpers=True, timeout=180)
def manual_forward_ownership(c):
    port = free_port()
    command = f'tcp 127.0.0.1 {port} {port}'
    c.write('ctx.forward add ' + command)
    c.ctx.forward('add ' + command)
    try:
        before = c.ctx.forward('list')
        n = c.container('/network_probe', 'tcp', '7777', '0.0.0.0', image=HELPERS,
                        create=True, opts=('-p', f'127.0.0.1:{port}:7777'))
        c.d('start', n, expected='failure')
        require(not c.inspect(n)['State']['Running'], 'Docker stole an existing host forwarding')
        c.d('rm', n)
        require(c.ctx.forward('list') == before, 'rollback deleted the manual forwarding')
    finally:
        c.write('ctx.forward del ' + command)
        c.ctx.forward('del ' + command)
    released(c, port)


from suites.functional import shared_files, shared_locks

for _group, _module in [('files', shared_files), ('locks', shared_locks)]:
    for _name in _module.NAMES:
        check('share.' + _group + '.' + _name, 'run', helpers=True, timeout=180)(
            lambda c, module=_module, name=_name: module.run(c, name))


@check('persistence.reboot', 'run|container inspect|container port|volume inspect|image inspect|container exec', helpers=True, timeout=240)
def persistence(c):
    volume = c.volume()
    image = c.object('image', 'persist')
    builder = c.container('sh', '-ec', 'printf image-payload >/image-data')
    c.d('commit', '--change', 'LABEL ' + c.label, builder, image)
    image_id = c.d('image', 'inspect', '-f', '{{.Id}}', image).strip()
    persistent = c.container('sh', '-ec', 'echo boot >>/data/boots; exec sleep 600',
                             opts=('--restart', 'always', '-v', volume + ':/data'), image=image, detached=True)
    c.d('exec', persistent, 'sh', '-ec', 'printf volume-payload >/data/payload; printf layer-payload >/layer-data')
    identity = c.inspect(persistent)['Id']
    stopped = c.container('sh', '-c', 'exit 19', create=True)
    c.d('start', stopped)
    require(c.d('wait', stopped).strip() == '19', 'stopped fixture exited with the wrong code')
    servers = []
    for udp in (False, True):
        port = free_port(udp)
        n = echo_server(c, f'127.0.0.1:{port}:7777/{"udp" if udp else "tcp"}', udp, opts=('--restart', 'always'))
        echo(c, port, udp)
        servers.append((n, port, udp))
    c.write('ctx.restart: orderly reboot using the same disk')
    c.ctx.restart()
    c.eventually(lambda: c.inspect(persistent)['State']['Running'], timeout=40)
    require(c.inspect(persistent)['Id'] == identity, 'container identity changed after reboot')
    require(c.d('image', 'inspect', '-f', '{{.Id}}', image).strip() == image_id, 'image lost after reboot')
    require(json.loads(c.d('volume', 'inspect', volume))[0]['Name'] == volume, 'volume lost after reboot')
    require(c.d('exec', persistent, 'cat', '/image-data', '/layer-data', '/data/payload') ==
            'image-payloadlayer-payloadvolume-payload', 'persistent contents differ')
    require(c.d('exec', persistent, 'cat', '/data/boots').splitlines() == ['boot', 'boot'], 'restart=always did not reexecute exactly once')
    state = c.inspect(stopped)['State']
    require(not state['Running'] and state['ExitCode'] == 19, 'stopped container state lost')
    for name, port, udp in servers:
        c.eventually(lambda: c.inspect(name)['State']['Running'], timeout=20)
        require(published(c, name, udp) == port, 'publish binding changed after reboot')
        echo(c, port, udp)
