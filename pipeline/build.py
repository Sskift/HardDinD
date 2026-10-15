"""Content-addressed builds. An artifact's key is the hash of every input that
can change its bytes; a complete store entry is never rebuilt or modified."""
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from pipeline import config

REPO = config.REPO
LOCK = json.loads((REPO / 'sources.lock.json').read_text())
VERBOSE = True


def log(message):
    if VERBOSE:
        print(f'[hd {time.strftime("%H:%M:%S")}] {message}', file=sys.stderr, flush=True)


def file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def key_of(*items):
    """Hash files by content (path relative to the repo) and other values by JSON."""
    h = hashlib.sha256()
    for item in items:
        if isinstance(item, pathlib.Path):
            name = str(item.relative_to(REPO)) if item.is_relative_to(REPO) else item.name
            h.update(f'file:{name}:{file_sha(item)}\n'.encode())
        else:
            h.update(('value:' + json.dumps(item, sort_keys=True) + '\n').encode())
    return h.hexdigest()[:20]


@contextlib.contextmanager
def lock(name, shared=False, wait=True):
    config.LOCKS.mkdir(parents=True, exist_ok=True)
    with open(config.LOCKS / f'{name}.lock', 'a') as f:
        flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(f, flags | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                raise RuntimeError(f'lock {name} is held by another run')
            log(f'waiting for lock {name}')
            fcntl.flock(f, flags)
        yield


def run(cmd, log_file=None, **kwargs):
    cmd = [str(c) for c in cmd]
    log(' '.join(cmd) if len(cmd) < 12 else ' '.join(cmd[:6]) + f' ... ({len(cmd)} args)')
    if log_file is None:
        subprocess.run(cmd, check=True, stdout=sys.stderr, **kwargs)
        return
    with open(log_file, 'a') as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, **kwargs)
    if result.returncode:
        tail = pathlib.Path(log_file).read_text(errors='replace').splitlines()[-25:]
        raise RuntimeError(f'command failed ({result.returncode}): {cmd[0]} ...; see {log_file}\n' + '\n'.join(tail))


def artifact(kind, key, builder):
    """Return STORE/kind/key, building it with builder(tmp, final) when absent."""
    final = config.STORE / kind / key
    if (final / '.complete').exists():
        os.utime(final)  # last use, for ./hd gc
        return final
    with lock('build'):
        if (final / '.complete').exists():
            return final
        tmp = config.STORE / kind / f'.{key}.tmp'
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        config.WORK.mkdir(parents=True, exist_ok=True)
        log(f'building {kind} {key}')
        started = time.time()
        builder(tmp, final)
        (tmp / '.complete').write_text(json.dumps({'kind': kind, 'key': key, 'seconds': round(time.time() - started, 1)}) + '\n')
        shutil.rmtree(final, ignore_errors=True)
        tmp.rename(final)
        log(f'built {kind} {key} in {time.time() - started:.0f}s')
    return final


def fetch(name):
    out = subprocess.run([sys.executable, REPO / 'scripts/fetch-artifact.py', name, config.WORK / 'cache'],
                         check=True, capture_output=True, text=True)
    return pathlib.Path(out.stdout.strip())


def series():
    names = [line.strip() for line in (REPO / 'kernel/series').read_text().splitlines()
             if line.strip() and not line.startswith('#')]
    return [REPO / 'kernel/patches' / n for n in names]


def candidate_paths(candidates):
    paths = []
    for name in candidates:
        path = REPO / 'kernel/candidates' / f'{name}.patch'
        if not path.exists():
            known = sorted(p.stem for p in (REPO / 'kernel/candidates').glob('*.patch'))
            raise SystemExit(f'unknown kernel candidate {name!r}; known: {", ".join(known)}')
        paths.append(path)
    return paths


def kernel(candidates=()):
    patches = series() + candidate_paths(candidates)
    key = key_of('kernel/1', LOCK['linux'], REPO / 'kernel/config', REPO / 'scripts/build-kernel.sh',
                 REPO / 'pipeline/patches.py', REPO / 'scripts/check-stub-relocations.py', *patches)
    def build(tmp, _final):
        run(['bash', REPO / 'scripts/build-kernel.sh', config.WORK, tmp / 'linux', *patches],
            log_file=tmp / 'build.log', env={**os.environ, 'JOBS': str(config.JOBS)})
    return artifact('kernel', key, build) / 'linux'


def network():
    inputs = [REPO / 'src/network_backend.c', REPO / 'src/network_probe.c', REPO / 'tests/publish/native_ip6frag.c',
              REPO / 'scripts/build-libslirp.sh', REPO / 'scripts/build-network.sh',
              *sorted((REPO / 'third_party/patches').glob('*.patch'))]
    key = key_of('network/1', LOCK['network_backend'], *inputs)
    def build(tmp, _final):
        run(['bash', REPO / 'scripts/build-network.sh', config.WORK], log_file=tmp / 'build.log')
        for name in ('network-backend', 'network-probe'):
            shutil.copy2(config.WORK / name, tmp / name)
    return artifact('network', key, build)


def moby():
    inputs = [REPO / 'scripts/build-moby.sh', REPO / 'scripts/fetch-moby.py', *sorted((REPO / 'moby/patches').glob('*.patch'))]
    key = key_of('moby/1', LOCK['moby_port_backend'], LOCK['go_build'], *inputs)
    def build(tmp, _final):
        run(['bash', REPO / 'scripts/build-moby.sh', config.WORK], log_file=tmp / 'build.log',
            env={**os.environ, 'JOBS': str(min(config.JOBS, 8))})
        for name in ('dockerd', 'docker-proxy'):
            shutil.copy2(config.WORK / 'moby-port-bin' / name, tmp / name)
    return artifact('moby', key, build)


def port_agent():
    inputs = [REPO / 'src/port_agent.go', REPO / 'scripts/build-port-agent.sh', REPO / 'scripts/fetch-moby.py']
    key = key_of('port-agent/1', LOCK['go_build'], *inputs)
    def build(tmp, _final):
        run(['bash', REPO / 'scripts/build-port-agent.sh', config.WORK], log_file=tmp / 'build.log')
        shutil.copy2(config.WORK / 'port-agent', tmp / 'port-agent')
    return artifact('port-agent', key, build) / 'port-agent'


def virtiofsd():
    lock_items = {k: v for k, v in LOCK.items() if k.startswith('virtiofs') or k == 'rust_build'}
    inputs = [REPO / 'scripts/build-virtiofs.sh', *sorted((REPO / 'virtiofs/patches').glob('*.patch'))]
    key = key_of('virtiofsd/1', lock_items, *inputs)
    def build(tmp, _final):
        run(['bash', REPO / 'scripts/build-virtiofs.sh', config.WORK, config.RUST_TOOLCHAIN], log_file=tmp / 'build.log')
        shutil.copy2(config.WORK / 'virtiofsd', tmp / 'virtiofsd')
    return artifact('virtiofsd', key, build) / 'virtiofsd'


def docker_root():
    """Pinned Alpine with the locked Docker packages; the base of rootfs and client."""
    key = key_of('docker-root/1', LOCK['alpine'], LOCK['alpine_packages'], LOCK['alpine_package_manifest'])
    def build(tmp, _final):
        archive = fetch('alpine')
        root = tmp / 'root'
        root.mkdir()
        run(['tar', '-xf', archive, '-C', root])
        shutil.copy2('/etc/resolv.conf', root / 'etc/resolv.conf')
        run(['timeout', '300', 'chroot', root, 'apk', 'add', '--no-cache', *LOCK['alpine_packages']],
            log_file=tmp / 'build.log')
        actual = set(subprocess.run(['chroot', root, 'apk', 'info', '-v'], check=True, capture_output=True,
                                    text=True).stdout.split())
        expected = set(LOCK['alpine_package_manifest'])
        if actual != expected:
            raise RuntimeError(f'Alpine packages drifted: missing {sorted(expected - actual)}, unexpected {sorted(actual - expected)}')
        (root / 'etc/resolv.conf').unlink()
    return artifact('docker-root', key, build) / 'root'


def rootfs():
    base = docker_root()
    bins = moby()
    agent = port_agent()
    guest = [REPO / 'guest/runtime-init', REPO / 'guest/runtime-daemon']
    key = key_of('rootfs/1', base.parent.name, bins.name, agent.parent.name, *guest)
    def build(tmp, _final):
        root = tmp / 'root'
        run(['cp', '-a', base, root])
        shutil.copy2(guest[0], root / 'sbin/harddind-runtime-init')
        shutil.copy2(guest[1], root / 'sbin/harddind-daemon')
        shutil.copy2(agent, root / 'sbin/harddind-port-agent')
        for name in ('dockerd', 'docker-proxy'):
            shutil.copy2(bins / name, root / 'usr/bin' / name)
        disk = tmp / 'rootfs.ext4'
        # Sparse; holds images, containers and build cache of one instance.
        run(['truncate', '-s', '24G', disk])
        run(['mkfs.ext4', '-q', '-F', '-d', root, disk], log_file=tmp / 'build.log')
        shutil.rmtree(root)
    return artifact('rootfs', key, build) / 'rootfs.ext4'


def client():
    """Outer Docker CLI with buildx and compose, matching the guest daemon."""
    base = docker_root()
    key = key_of('client/1', base.parent.name, LOCK['docker_compose'])
    def build(tmp, final):
        shutil.copy2(base / 'lib/ld-musl-x86_64.so.1', tmp / 'ld-musl-x86_64.so.1', follow_symlinks=True)
        shutil.copy2(base / 'usr/bin/docker', tmp / 'docker')
        shutil.copy2(base / 'usr/libexec/docker/cli-plugins/docker-buildx', tmp / 'buildx')
        plugins = tmp / 'cli-plugins'
        plugins.mkdir()
        loader, buildx = final / 'ld-musl-x86_64.so.1', final / 'buildx'
        (plugins / 'docker-buildx').write_text(f'#!/bin/sh\nexec {loader} {buildx} "$@"\n')
        (plugins / 'docker-buildx').chmod(0o755)
        shutil.copy2(fetch('docker_compose'), plugins / 'docker-compose')
        (plugins / 'docker-compose').chmod(0o755)
    return artifact('client', key, build)


def everything(candidates=(), shares=False):
    """Build what an instance needs; returns paths plus the ids that identify the build."""
    kern = kernel(candidates)
    result = {
        'kernel': kern,
        'rootfs': rootfs(),
        'network_backend': network() / 'network-backend',
        'network_probe': network() / 'network-probe',
        'client': client(),
        'alpine_tar': fetch('alpine'),
        'virtiofsd': virtiofsd() if shares else None,
    }
    ids = {name: path.parent.name if path.is_file() else path.name
           for name, path in result.items() if isinstance(path, pathlib.Path) and name != 'alpine_tar'}
    result['ids'] = ids
    result['candidates'] = list(candidates)
    return result
