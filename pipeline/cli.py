"""./hd — the only entry point for building, running, testing and measuring.

Local commands (developer machine) talk to Starpub over one multiplexed ssh
connection; everything else runs on the Linux host that holds the build store.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import traceback

from pipeline import config

REPO = config.REPO
RSYNC_EXCLUDES = ['.git/', '.work/', '__pycache__/', '*.pyc', '/results/runs/']
# Each checkout (worktree) syncs to its own remote directory; store, runs and
# locks are shared, so parallel agents never overwrite each other's sources.
CHECKOUT = os.environ.get('HD_CHECKOUT') or ''.join(c if c.isalnum() or c in '-_' else '-' for c in REPO.name)
REMOTE_SRC = f'{config.REMOTE_ROOT}/src/{CHECKOUT}'


# ---------------------------------------------------------------- local side

def require_master():
    """Refuse to open bare TCP connections: the jump host bans bursts of them."""
    if config.JUMP and subprocess.run(['ssh', '-O', 'check', config.JUMP], capture_output=True).returncode:
        raise SystemExit(f'ssh master for {config.JUMP} is down; wait for it to reconnect (launchd keepalive) '
                         'instead of retrying, or set HD_JUMP= to skip this check')


def ssh(script, check=True, capture=False):
    """Run one shell script remotely in a single ssh invocation."""
    require_master()
    result = subprocess.run(['ssh', config.REMOTE, 'bash -s'], input=script, text=True,
                            capture_output=capture)
    if check and result.returncode:
        raise SystemExit(f'remote command failed ({result.returncode})' + (f': {result.stderr[-500:]}' if capture else ''))
    return result


def git(*args):
    return subprocess.run(['git', '-C', REPO, *args], check=True, capture_output=True, text=True).stdout.strip()


def cmd_sync(_a=None):
    status = git('status', '--porcelain', '--untracked-files=no')
    diff = git('diff', 'HEAD')
    rev = {'git': git('rev-parse', 'HEAD'), 'dirty': bool(status), 'subject': git('log', '-1', '--format=%s'),
           'diff_sha256': hashlib.sha256(diff.encode()).hexdigest() if diff else None,
           'synced_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    (REPO / '.hd-revision.json').write_text(json.dumps(rev, indent=2) + '\n')
    cmd = ['rsync', '-az', '--delete', f'--rsync-path=mkdir -p {REMOTE_SRC} && rsync',
           *[f'--exclude={e}' for e in RSYNC_EXCLUDES], f'{REPO}/', f'{config.REMOTE}:{REMOTE_SRC}/']
    require_master()
    subprocess.run(cmd, check=True)
    print(f"synced {rev['git'][:12]}{' (dirty)' if rev['dirty'] else ''} -> {config.REMOTE}:{REMOTE_SRC}")


def cmd_remote(a):
    cmd_sync()
    args = ' '.join(shlex.quote(x) for x in a.args)
    sys.exit(ssh(f'cd {REMOTE_SRC} && ./hd {args}', check=False).returncode)


def cmd_job(a):
    """Run `./hd ARGS` on Starpub inside tmux; returns immediately."""
    if not a.name.replace('-', '').replace('_', '').isalnum():
        raise SystemExit('job name must be alphanumeric with - or _')
    cmd_sync()
    args = ' '.join(shlex.quote(x) for x in a.args)
    root, name = config.REMOTE_ROOT, a.name
    # Each job runs from its own copy so later syncs cannot change code under it.
    snapshot = f'{root}/jobs/{name}'
    inner = f'cd {snapshot} && ./hd {args} > {root}/logs/{name}.log 2>&1; echo "[hd] job exit $?" >> {root}/logs/{name}.log'
    ssh(f'''set -e
mkdir -p {root}/logs {root}/jobs
if tmux has-session -t hd-{name} 2>/dev/null; then echo "job {name} is still running" >&2; exit 3; fi
[ ! -e {root}/logs/{name}.log ] || mv {root}/logs/{name}.log {root}/logs/{name}.log.1
rm -rf {snapshot} && cp -a {REMOTE_SRC} {snapshot}
tmux new-session -d -s hd-{name} {shlex.quote(inner)}
echo "started job {name}: ./hd {args}"
''')


def cmd_jobs(_a):
    root = config.REMOTE_ROOT
    ssh(f'''cd {root}/logs 2>/dev/null || exit 0
running=$(tmux ls -F '#{{session_name}}' 2>/dev/null | sed -n 's/^hd-//p')
for f in $(ls -t *.log 2>/dev/null | head -12); do
  n=${{f%.log}}; s=done; echo " $running " | grep -q " $n " && s=running
  printf '%-24s %-8s %s\\n' "$n" "$s" "$(tail -n 1 "$f" | cut -c1-110)"
done
''')


def cmd_log(a):
    ssh(f'tail -n {int(a.lines)} {config.REMOTE_ROOT}/logs/{a.name}.log')


def cmd_wait(a):
    """Block until a job ends, then show its tail. One ssh session, polled remotely."""
    root, name = config.REMOTE_ROOT, a.name
    ssh(f'while tmux has-session -t hd-{name} 2>/dev/null; do sleep 15; done; tail -n {int(a.lines)} {root}/logs/{name}.log')


def cmd_fetch(_a):
    dest = REPO / 'results/runs'
    dest.mkdir(parents=True, exist_ok=True)
    require_master()
    subprocess.run(['rsync', '-a', '--prune-empty-dirs', '--include=*/', '--include=summary.json',
                    '--include=scorecard.md', '--exclude=*', f'{config.REMOTE}:{config.REMOTE_ROOT}/runs/', f'{dest}/'],
                   check=True)
    runs = sorted(p.parent.name for p in dest.glob('*/summary.json'))
    print(f'{len(runs)} run summaries in results/runs (not tracked); latest: {runs[-1] if runs else "-"}')
    # Tracked history: complete ci runs only (no --only, no --quick, no run error).
    from pipeline import score
    complete = []
    for name in runs:
        summary = json.loads((dest / name / 'summary.json').read_text())
        if summary.get('kind') == 'ci' and not summary.get('only') and not summary.get('quick') and not summary.get('error'):
            complete.append(name)
            target = REPO / 'results/ci' / name
            if not target.exists():
                target.mkdir(parents=True)
                for f in ('summary.json', 'scorecard.md'):
                    shutil.copy2(dest / name / f, target / f)
                print(f'results/ci/{name} added')
    if complete:
        shutil.copy2(dest / complete[-1] / 'scorecard.md', REPO / 'results/SCOREBOARD.md')
        rows = ['| run | revision | functional | key | important | other |', '|---|---|---|---|---|---|']
        for name in reversed(complete):
            card = score.scorecard(score.load(REPO / 'results/ci' / name / 'summary.json'))
            t, tiers = card.get('test', {}), card.get('tiers', {})
            rows.append(f"| {name} | {card['revision'].get('git', '?')[:8]} | {t.get('pass', 0)}/{t.get('checks', 0)} | " +
                        ' | '.join(f"{tiers.get(k, {}).get('pass', 0)}/{tiers.get(k, {}).get('total', 0)}" for k in score.TIER_ORDER) + ' |')
        (REPO / 'results/HISTORY.md').write_text('# Complete ci runs, newest first\n\n' + '\n'.join(rows) + '\n')
        print(f'results/SCOREBOARD.md <- {complete[-1]}')


def cmd_check(_a):
    """Fast local checks; no Linux host needed. Exits non-zero on any failure."""
    steps = [[sys.executable, '-m', 'unittest', 'discover', '-q', '-s', str(REPO / 'tests')],
             [sys.executable, '-m', 'compileall', '-q', str(REPO / 'pipeline'), str(REPO / 'scripts')]]
    steps += [['bash', '-n', str(p)] for p in sorted((REPO / 'scripts').glob('*.sh'))]
    steps += [['sh', '-n', str(p)] for p in sorted((REPO / 'guest').iterdir())]
    for step in steps:
        if subprocess.run(step, cwd=REPO).returncode:
            raise SystemExit('check failed: ' + ' '.join(step[-2:]))
    print('check: ok')


# --------------------------------------------------------------- remote side

def require_linux():
    if sys.platform != 'linux':
        raise SystemExit('this command runs on the Linux build host; use ./hd remote ... or ./hd job NAME -- ...')


def cmd_build(a):
    require_linux()
    from pipeline import build
    artifacts = build.everything(a.with_, shares=a.shares)
    print(json.dumps(artifacts, indent=2, default=str))


def cmd_up(a):
    require_linux()
    from pipeline import build, instance, runs
    artifacts = build.everything(a.with_, shares=bool(a.share))
    opts = runs.instance_options(artifacts, cpuset=a.cpuset, kernel_args=a.kernel_arg, shares=a.share,
                                 cpus=a.cpus, memory=a.memory)
    path = config.INSTANCES / a.name
    if a.recreate and path.exists():
        instance.stop(path); instance.remove(path)
    state = instance.start(path, hold_measure=True, **opts)
    print(f"{a.name}: ready, API 127.0.0.1:{state['api_port']}; use ./hd docker {a.name} -- ...")


def cmd_down(a):
    require_linux()
    from pipeline import instance
    path = config.INSTANCES / a.name
    state = instance.stop(path)
    print(f"{a.name}: {state.get('phase')}{' ' + state['error'] if state.get('error') else ''}")
    if a.rm:
        instance.remove(path)


def cmd_docker(a):
    require_linux()
    from pipeline import instance
    argv, env = instance.docker_env(config.INSTANCES / a.name)
    args = a.args[1:] if a.args[:1] == ['--'] else a.args
    os.execve(argv[0], [*argv, *args], env)


def cmd_ps(_a):
    require_linux()
    from pipeline import instance
    for path in sorted(config.INSTANCES.glob('*')) if config.INSTANCES.exists() else []:
        state = instance.read_state(path)
        alive = instance.live(state)
        print(f"{path.name:28s} {state.get('phase', '?'):9s} {'live' if alive else 'dead':5s} "
              f"api={state.get('api_port', '-')} {state.get('error', '')}")


def cmd_gc(a):
    """Drop old runs, dead instances and store entries no kept run refers to."""
    require_linux()
    from pipeline import build, instance
    from pipeline import locks
    with build.lock('build'), locks.measure():
        runs = sorted(p for p in config.RUNS.glob('*') if p.is_dir()) if config.RUNS.exists() else []
        drop = runs[:-a.keep_runs] if len(runs) > a.keep_runs else []
        for path in drop:
            shutil.rmtree(path)
        used = set()
        for path in runs[len(drop):]:
            try:
                used.update(json.loads((path / 'summary.json').read_text()).get('artifacts', {}).values())
            except (OSError, ValueError):
                pass
        cutoff = time.time() - a.keep_days * 86400
        removed = []
        for entry in config.STORE.glob('*/*') if config.STORE.exists() else []:
            if entry.name.startswith('.') or (entry.name not in used and entry.stat().st_mtime < cutoff):
                shutil.rmtree(entry); removed.append(f'{entry.parent.name}/{entry.name}')
        for path in config.INSTANCES.glob('*') if config.INSTANCES.exists() else []:
            if not instance.live(instance.read_state(path)) and path.stat().st_mtime < cutoff:
                instance.remove(path); removed.append('instance/' + path.name)
    print(f'gc: removed {len(drop)} runs and {len(removed)} store/instance entries')


def run_suite(kind, a):
    """Shared driver for test/bench/ci: build, boot, run the suite(s), score.

    The functional phase holds the measure lock shared; the bench phase holds it
    exclusively, so measurements never overlap another guest of this pipeline."""
    require_linux()
    from pipeline import build, locks, runs, score
    run_id, run_dir, rev = runs.new_run(kind, a.label)
    print(f'run {run_id}', flush=True)
    summary = {'schema': 'hd.summary/1', 'run_id': run_id, 'kind': kind, 'revision': rev,
               'candidates': a.with_, 'kernel_args': a.kernel_arg, 'started': time.time(),
               'host': runs.host_info(), 'only': a.only, 'quick': getattr(a, 'quick', False)}
    try:
        artifacts = build.everything(a.with_, shares=True)
        summary['artifacts'] = artifacts['ids']
        if kind in ('test', 'ci'):
            with locks.measure(shared=True):
                summary['test'] = with_guest(run_id, run_dir, artifacts, a, 'test', cpuset=None)
            summary['test']['checks'].insert(0, backend_selftest(run_dir, artifacts, a.only))
            summary['test']['checks'].append(shutdown_check(summary['test'].pop('guest_state')))
        if kind in ('bench', 'ci'):
            with locks.measure():
                cpus = runs.pick_cpus(config.GUEST_CPUS)
                summary['bench_cpus'] = cpus
                summary['bench'] = with_guest(run_id, run_dir, artifacts, a, 'bench', cpuset=cpus)
            state = summary['bench'].pop('guest_state')
            if shutdown_check(state)['status'] != 'pass':
                summary['error'] = 'bench guest did not shut down cleanly: ' + shutdown_check(state)['detail']
    except Exception as error:
        summary['error'] = f'{type(error).__name__}: {error}'
        traceback.print_exc()
    summary['finished'] = time.time()
    summary['host_end'] = runs.host_info()
    runs.write_json(run_dir / 'summary.json', summary)
    card = score.scorecard(summary)
    previous = latest_card(kind, exclude=run_id)
    text = score.markdown(card, previous)
    if summary.get('error'):
        text += f"\n**Run error**: {summary['error']}\n"
    (run_dir / 'scorecard.md').write_text(text)
    print(text)
    return 2 if summary.get('error') else 0


def backend_selftest(run_dir, artifacts, only):
    """Native tests of the network backend binary (tests/publish); no guest needed."""
    import re
    check = {'id': 'network_backend_selftest', 'covers': [], 'seconds': 0.0}
    if only and not re.search(only, check['id']):
        return {**check, 'status': 'not_tested'}
    started = time.monotonic()
    env = {**os.environ, 'HARDDIND_NETWORK_BACKEND': str(artifacts['network_backend'])}
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(REPO / 'tests/publish')],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
    (run_dir / 'network-backend-selftest.log').write_text(result.stdout + result.stderr)
    return {**check, 'status': 'pass' if result.returncode == 0 else 'fail',
            'seconds': round(time.monotonic() - started, 2), 'detail': (result.stdout + result.stderr)[-800:]}


def shutdown_check(state):
    """An orderly guest shutdown is part of the functional gate (daemon state must persist)."""
    problems = [k for k in ('error', 'shutdown_error') if state.get(k)]
    if state.get('forced_shutdown'):
        problems.append('forced_shutdown')
    if state.get('phase') != 'stopped':
        problems.append(f"phase={state.get('phase')}")
    detail = '; '.join(f'{k}: {state[k]}' if k in state and k != 'forced_shutdown' else k for k in problems)
    return {'id': 'guest.clean_shutdown', 'status': 'fail' if problems else 'pass', 'covers': [],
            'seconds': round(state.get('stopped_at', 0) - state.get('ready_at', 0), 2) if not problems else 0.0,
            'detail': detail}


def with_guest(run_id, run_dir, artifacts, a, suite, cpuset):
    from pipeline import instance, runs
    name = suite[0] + hashlib.sha256(run_id.encode()).hexdigest()[:10]
    path = config.INSTANCES / name
    share = run_dir / f'share-{suite}'
    share.mkdir()
    shares = [share]
    opts = runs.instance_options(artifacts, cpuset=cpuset, kernel_args=a.kernel_arg, shares=shares)
    ctx = runs.Context(run_id=run_id, run_dir=run_dir, instance=path, artifacts=artifacts,
                       instance_options=opts, bench_cpus=cpuset, quick=getattr(a, 'quick', False),
                       only=a.only, share=share)
    result = {}
    try:
        started = time.monotonic()
        instance.start(path, **opts)
        ctx.log(f'{suite}: guest {name} ready in {time.monotonic() - started:.1f}s')
        if suite == 'test':
            from pipeline import functional
            result = functional.run(ctx)
        else:
            from pipeline import bench
            result = bench.run(ctx)
        return result
    finally:
        state = instance.stop(path)
        result['guest_state'] = state
        keep = run_dir / f'instance-{suite}'
        keep.mkdir(exist_ok=True)
        for log in [*path.glob('*.log'), *path.glob('shutdown-diagnostic-*.json')]:
            shutil.copy2(log, keep / log.name)
        (keep / 'state.json').write_text(json.dumps(state, indent=2) + '\n')
        if not getattr(a, 'keep', False):
            instance.remove(path)
        ctx.log(f'{suite}: guest {name} {state.get("phase")}')


def latest_card(kind, exclude):
    from pipeline import score
    candidates = sorted(p for p in config.RUNS.glob(f'*-{kind}*/summary.json') if p.parent.name != exclude)
    for path in reversed(candidates):
        try:
            return score.scorecard(score.load(path))
        except (OSError, ValueError, KeyError):
            continue
    return None


def find_summary(ref):
    for path in (pathlib.Path(ref), pathlib.Path(ref) / 'summary.json', config.RUNS / ref / 'summary.json',
                 REPO / 'results/runs' / ref / 'summary.json'):
        if path.is_file():
            return path
    raise SystemExit(f'no summary for {ref}')


def cmd_probe(a):
    require_linux()
    from pipeline import probe
    sys.exit(probe.run(a))


def cmd_score(a):
    from pipeline import score
    card = score.scorecard(score.load(find_summary(a.run)))
    previous = score.scorecard(score.load(find_summary(a.vs))) if a.vs else None
    if a.json:
        print(json.dumps(card, indent=2))
    else:
        print(score.markdown(card, previous))


def main(argv=None):
    p = argparse.ArgumentParser(prog='hd', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)

    def runopts(s, bench=False):
        s.add_argument('--with', dest='with_', action='append', default=[], metavar='CANDIDATE',
                       help='apply kernel/candidates/CANDIDATE.patch after the series (repeatable)')
        s.add_argument('--kernel-arg', action='append', default=[], metavar='K=V', help='extra UML boot parameter')
        s.add_argument('--only', metavar='REGEX', help='run only matching checks/metrics; the rest count as not tested')
        s.add_argument('--label', help='short tag appended to the run id')
        s.add_argument('--keep', action='store_true', help='keep the guest disk after the run')
        if bench:
            s.add_argument('--quick', action='store_true', help='fewer, shorter samples (smoke only, not for decisions)')

    sub.add_parser('sync', help='[local] rsync this tree to Starpub').set_defaults(fn=cmd_sync)
    s = sub.add_parser('remote', help='[local] sync, then run ./hd ARGS on Starpub in the foreground')
    s.add_argument('args', nargs=argparse.REMAINDER); s.set_defaults(fn=cmd_remote)
    s = sub.add_parser('job', help='[local] sync, then run ./hd ARGS on Starpub in tmux (detached)')
    s.add_argument('name'); s.add_argument('args', nargs=argparse.REMAINDER); s.set_defaults(fn=cmd_job)
    sub.add_parser('jobs', help='[local] list recent remote jobs').set_defaults(fn=cmd_jobs)
    s = sub.add_parser('log', help='[local] tail a remote job log')
    s.add_argument('name'); s.add_argument('-n', '--lines', default=40); s.set_defaults(fn=cmd_log)
    s = sub.add_parser('wait', help='[local] wait for a remote job to finish')
    s.add_argument('name'); s.add_argument('-n', '--lines', default=60); s.set_defaults(fn=cmd_wait)
    sub.add_parser('fetch', help='[local] copy run summaries into results/runs').set_defaults(fn=cmd_fetch)
    sub.add_parser('check', help='fast local unit and syntax checks').set_defaults(fn=cmd_check)

    s = sub.add_parser('build', help='build (or reuse) every artifact')
    s.add_argument('--with', dest='with_', action='append', default=[], metavar='CANDIDATE')
    s.add_argument('--shares', action='store_true', help='also build virtiofsd'); s.set_defaults(fn=cmd_build)
    s = sub.add_parser('up', help='boot a named guest for interactive work')
    s.add_argument('name'); s.add_argument('--with', dest='with_', action='append', default=[], metavar='CANDIDATE')
    s.add_argument('--kernel-arg', action='append', default=[]); s.add_argument('--share', action='append', default=[])
    s.add_argument('--cpuset'); s.add_argument('--cpus', type=int); s.add_argument('--memory')
    s.add_argument('--recreate', action='store_true', help='discard an existing instance of this name')
    s.set_defaults(fn=cmd_up)
    s = sub.add_parser('down', help='stop a named guest')
    s.add_argument('name'); s.add_argument('--rm', action='store_true', help='also delete its disk'); s.set_defaults(fn=cmd_down)
    s = sub.add_parser('docker', help='run the Docker CLI against a named guest')
    s.add_argument('name'); s.add_argument('args', nargs=argparse.REMAINDER); s.set_defaults(fn=cmd_docker)
    sub.add_parser('ps', help='list guests').set_defaults(fn=cmd_ps)
    s = sub.add_parser('gc', help='prune old runs, unused store entries and dead guests')
    s.add_argument('--keep-runs', type=int, default=60); s.add_argument('--keep-days', type=float, default=3)
    s.set_defaults(fn=cmd_gc)
    for kind, text in (('test', 'functional Docker suite on a fresh guest'),
                       ('bench', 'performance matrix, native vs guest, exclusive'),
                       ('ci', 'test then bench in one run')):
        s = sub.add_parser(kind, help=text); runopts(s, bench=kind != 'test')
        s.set_defaults(fn=lambda a, kind=kind: sys.exit(run_suite(kind, a)))
    s = sub.add_parser('probe', help='run one static program natively and in guests, interleaved (kernel A/B)')
    s.add_argument('--with', dest='with_', action='append', default=[], metavar='CANDIDATE')
    s.add_argument('--kernel-arg', action='append', default=[], metavar='K=V', help='boot parameter for every guest')
    s.add_argument('--ab', action='append', default=[], metavar='K=V', help='also boot guest B with these parameters')
    s.add_argument('--reps', type=int, default=3); s.add_argument('--timeout', type=float, default=300)
    s.add_argument('--privileged', action='store_true'); s.add_argument('--label')
    s.add_argument('program', help='repo C file (built static) or static binary path')
    s.add_argument('args', nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_probe)
    s = sub.add_parser('score', help='print a scorecard for a run')
    s.add_argument('run'); s.add_argument('--vs', help='compare with another run'); s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_score)
    a = p.parse_args(argv)
    if getattr(a, 'args', None) and a.args[:1] == ['--'] and a.command in ('remote', 'job'):
        a.args = a.args[1:]
    return a.fn(a)
