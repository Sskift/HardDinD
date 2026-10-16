"""Run directories, revision identity and the context handed to suites."""
import dataclasses
import json
import os
import pathlib
import platform
import re
import subprocess
import time

from pipeline import config, instance

REVISION_FILE = config.REPO / '.hd-revision.json'


def revision():
    """Identity of the source tree: written by `hd sync`, else read from git."""
    if REVISION_FILE.exists():
        return json.loads(REVISION_FILE.read_text())
    try:
        git = lambda *a: subprocess.run(['git', '-C', config.REPO, *a], check=True, capture_output=True, text=True).stdout.strip()
        sha = git('rev-parse', 'HEAD')
        dirty = bool(git('status', '--porcelain', '--untracked-files=no'))
        return {'git': sha, 'dirty': dirty, 'subject': git('log', '-1', '--format=%s')}
    except (OSError, subprocess.CalledProcessError):
        return {'git': 'unknown', 'dirty': True, 'subject': ''}


def throttle():
    """(quota periods, throttled periods) of the outer container's CPU quota (cgroup v2)."""
    try:
        stat = dict(line.split() for line in pathlib.Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
        return int(stat['nr_periods']), int(stat['nr_throttled'])
    except (OSError, KeyError, ValueError):
        return None


def throttled_fraction(seconds=20):
    a = throttle(); time.sleep(seconds); b = throttle()
    if not a or not b or b[0] <= a[0]:
        return 0.0
    return (b[1] - a[1]) / (b[0] - a[0])


def wait_quiet(limit=0.10, max_wait=None, log=print):
    """Wait until the outer container is rarely throttled; returns what was observed.

    Benchmarks taken while the 36-CPU container quota throttles us mostly
    measure other tenants. Gives up after max_wait seconds (HD_QUIET_WAIT)."""
    max_wait = config.QUIET_WAIT if max_wait is None else max_wait
    started = time.monotonic(); observed = []
    while True:
        fraction = throttled_fraction()
        observed.append(round(fraction, 3))
        if fraction <= limit:
            return {'quiet': True, 'waited': round(time.monotonic() - started), 'throttled': observed}
        if time.monotonic() - started > max_wait:
            return {'quiet': False, 'waited': round(time.monotonic() - started), 'throttled': observed}
        log(f'host throttled in {fraction:.0%} of quota periods; waiting for a quieter window')
        time.sleep(40)


def host_info():
    info = {'node': platform.node(), 'kernel': platform.release(), 'nproc': os.cpu_count(),
            'loadavg': os.getloadavg(), 'throttle': throttle()}
    try:
        info['cpu_max'] = pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip()
    except OSError:
        pass
    try:
        model = re.search(r'^model name\s*:\s*(.*)$', pathlib.Path('/proc/cpuinfo').read_text(), re.M)
        info['cpu'] = model.group(1) if model else None
    except OSError:
        pass
    return info


def cpu_busy(seconds=2.0):
    """Busy fraction per CPU over a short window, from /proc/stat."""
    def sample():
        rows = {}
        for line in pathlib.Path('/proc/stat').read_text().splitlines():
            if line.startswith('cpu') and line[3:4].isdigit():
                name, *values = line.split()
                values = list(map(int, values))
                rows[int(name[3:])] = (sum(values), values[3] + values[4])  # total, idle + iowait
        return rows
    before = sample(); time.sleep(seconds); after = sample()
    return {cpu: 1 - (after[cpu][1] - before[cpu][1]) / max(1, after[cpu][0] - before[cpu][0])
            for cpu in after if cpu in before}


def pick_cpus(count):
    """The `count` quietest physical cores right now, one hyperthread each.

    Starpub is shared with tenants whose load moves around; a fixed CPU list
    measures their jobs as much as ours. HD_BENCH_CPUS pins an explicit list."""
    if config.BENCH_CPUS != 'auto':
        return config.BENCH_CPUS
    allowed = os.sched_getaffinity(0)
    busy = cpu_busy()
    def topology(cpu, name, default):
        try:
            return pathlib.Path(f'/sys/devices/system/cpu/cpu{cpu}/topology/{name}').read_text().strip()
        except OSError:
            return default
    packages = {}  # package -> {core siblings -> load of the whole core}
    for cpu in sorted(allowed & set(busy)):
        siblings = topology(cpu, 'thread_siblings_list', str(cpu))
        load = sum(busy.get(c, 1) for c in instance.parse_cpus(siblings))
        packages.setdefault(topology(cpu, 'physical_package_id', '0'), {}).setdefault(siblings, (load, cpu))
    best = None
    for cores in packages.values():  # one socket: no cross-socket memory traffic
        quiet = sorted(cores.values())[:count]
        if len(quiet) == count and (best is None or sum(l for l, _ in quiet) < sum(l for l, _ in best)):
            best = quiet
    if best is None:
        raise RuntimeError(f'no package has {count} cores available for benchmarking')
    return ','.join(str(cpu) for _, cpu in sorted(best, key=lambda item: item[1]))


def new_run(kind, label=None):
    rev = revision()
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    parts = [stamp, rev['git'][:8] + ('-dirty' if rev.get('dirty') else ''), kind]
    if label:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,40}', label):
            raise SystemExit('label must be [A-Za-z0-9_.-], at most 41 characters')
        parts.append(label)
    run_id = '-'.join(parts)
    path = config.RUNS / run_id
    path.mkdir(parents=True)
    return run_id, path, rev


@dataclasses.dataclass
class Context:
    """What a suite gets: a ready guest, its build, and where to put results."""
    run_id: str
    run_dir: pathlib.Path
    instance: pathlib.Path
    artifacts: dict
    instance_options: dict
    bench_cpus: str
    quick: bool = False
    only: str = None
    share: pathlib.Path = None  # outer directory mounted at the same path in the guest

    def selected(self, name):
        return self.only is None or re.search(self.only, name) is not None

    def log(self, message):
        line = f'[{time.strftime("%H:%M:%S")}] {message}'
        print(line, flush=True)
        with open(self.run_dir / 'run.log', 'a') as f:
            f.write(line + '\n')

    def docker_cmd(self):
        """(argv prefix, env) for the outer Docker CLI connected to the guest."""
        return instance.docker_env(self.instance)

    def docker(self, *args, input=None, timeout=120, check=False, text=True):
        argv, env = self.docker_cmd()
        try:
            result = subprocess.run([*argv, *map(str, args)], input=input, env=env, capture_output=True,
                                    text=text, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f'docker {" ".join(map(str, args[:3]))}: timed out after {timeout}s') from error
        if check and result.returncode:
            raise RuntimeError(f'docker {" ".join(map(str, args[:4]))} exited {result.returncode}: '
                               f'{(result.stderr or "")[-400:]}')
        return result

    def restart(self):
        """Orderly guest shutdown and boot on the same disk (daemon persistence checks)."""
        state = instance.stop(self.instance)
        if state.get('phase') != 'stopped':
            raise RuntimeError('guest did not stop cleanly: ' + str(state.get('error')))
        instance.start(self.instance, fresh=False, **self.instance_options)

    def forward(self, command):
        """Network backend control: e.g. 'add tcp 127.0.0.1 8080 80'."""
        return instance.control(self.instance, command)


def instance_options(artifacts, cpuset=None, kernel_args=(), shares=(), cpus=None, memory=None, share_opts=()):
    return {
        'kernel': artifacts['kernel'], 'disk': artifacts['rootfs'],
        'network_backend': artifacts['network_backend'], 'client': artifacts['client'],
        'virtiofsd': artifacts.get('virtiofsd'), 'cpus': cpus or config.GUEST_CPUS,
        'memory': memory or config.GUEST_MEMORY, 'cpuset': cpuset, 'kernel_args': list(kernel_args),
        'shares': list(shares),
        # notify_changes: virtiofsd watches the outer tree and invalidates guest caches (patch 0035).
        'share_options': {'posix_locks': True, 'notify_changes': True, **share_options(share_opts)} if shares else None,
        # vnet link: GSO/checksum offload and batched backend I/O (kernel patch 0034).
        'net_vnet': True, 'network_batch': 16,
    }



def share_options(pairs):
    """--share-opt K=V: true/false, integers, or strings (virtiofsd options, default off)."""
    result = {}
    for pair in pairs:
        key, _, value = pair.partition('=')
        result[key] = {'true': True, 'false': False}.get(value, int(value) if value.isdigit() else value)
    return result


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=str) + '\n')
    tmp.replace(path)
