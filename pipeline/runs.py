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


def host_info():
    info = {'node': platform.node(), 'kernel': platform.release(), 'nproc': os.cpu_count(),
            'loadavg': os.getloadavg()}
    try:
        model = re.search(r'^model name\s*:\s*(.*)$', pathlib.Path('/proc/cpuinfo').read_text(), re.M)
        info['cpu'] = model.group(1) if model else None
    except OSError:
        pass
    return info


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


def instance_options(artifacts, cpuset=None, kernel_args=(), shares=(), cpus=None, memory=None):
    return {
        'kernel': artifacts['kernel'], 'disk': artifacts['rootfs'],
        'network_backend': artifacts['network_backend'], 'client': artifacts['client'],
        'virtiofsd': artifacts.get('virtiofsd'), 'cpus': cpus or config.GUEST_CPUS,
        'memory': memory or config.GUEST_MEMORY, 'cpuset': cpuset, 'kernel_args': list(kernel_args),
        'shares': list(shares),
        'share_options': {'posix_locks': True} if shares else None,
    }


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=str) + '\n')
    tmp.replace(path)
