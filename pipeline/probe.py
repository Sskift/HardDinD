"""Run one static program natively and in containers, interleaved, for kernel work.

`./hd probe [--with C] [--kernel-arg K=V] [--ab K=V] [--reps N] [--privileged] PROGRAM [ARGS...]`
PROGRAM is a C file in the repo (built static, cached) or a path to a static binary.
Every rep runs: native, guest A, [guest B]. Guest B is the same kernel booted with the
--ab parameters, so a default-off candidate is compared against itself.
"""
import io
import hashlib
import json
import os
import pathlib
import subprocess
import tarfile
import time

from pipeline import build, config, instance, locks, runs

FLAGS = ['-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread']


def program(path):
    path = pathlib.Path(path)
    source = path if path.is_absolute() else config.REPO / path
    if source.suffix != '.c':
        return source.resolve()
    key = build.key_of('probe/1', FLAGS, source)
    def compile_(tmp, _final):
        build.run(['gcc', *FLAGS, source, '-o', tmp / source.stem, '-lm'], log_file=tmp / 'build.log')
    return build.artifact('probe', key, compile_) / source.stem


def image_tar(binary):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for name in ('tmp', 'dev', 'proc', 'sys', 'etc'):
            info = tarfile.TarInfo(name); info.type = tarfile.DIRTYPE; info.mode = 0o1777 if name == 'tmp' else 0o755
            tar.addfile(info)
        tar.add(binary, arcname='probe')
    return data.getvalue()


def last_json(text):
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def instance_path(run_id, side):
    return config.INSTANCES / ('p' + side.lower() + hashlib.sha256(run_id.encode()).hexdigest()[:12])


def run(a):
    binary = program(a.program)
    artifacts = build.everything(a.with_)
    cpus = runs.instance_options(artifacts)['cpus']
    with locks.measure():
        bench_cpus = runs.pick_cpus(cpus)
        affinity = instance.parse_cpus(bench_cpus)
        run_id, run_dir, rev = runs.new_run('probe', a.label)
        print(f'run {run_id}', flush=True)
        guests = {'A': list(a.kernel_arg)}
        if a.ab:
            guests['B'] = list(a.kernel_arg) + list(a.ab)
        paths = {}
        records = []
        try:
            for side, args in guests.items():
                path = instance_path(run_id, side)
                paths[side] = path
                instance.start(path, **runs.instance_options(artifacts, cpuset=bench_cpus, kernel_args=args))
                argv, env = instance.docker_env(path)
                subprocess.run([*argv, 'import', '-', 'hd-probe:latest'], input=image_tar(binary), env=env,
                               check=True, capture_output=True, timeout=120)
            for rep in range(a.reps):
                sides = [('native', None)] + [(side, path) for side, path in paths.items()]
                for side, path in sides:
                    if path is None:
                        cmd, env = [str(binary), *a.args], None
                        preexec = lambda: os.sched_setaffinity(0, affinity)
                    else:
                        argv, env = instance.docker_env(path)
                        cmd = [*argv, 'run', '--rm', *(['--privileged'] if a.privileged else []),
                               'hd-probe:latest', '/probe', *a.args]
                        preexec = None
                    started = time.monotonic()
                    try:
                        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=a.timeout,
                                                preexec_fn=preexec)
                        rc, out, err = result.returncode, result.stdout, result.stderr
                    except subprocess.TimeoutExpired as error:
                        rc, out, err = 'timeout', error.stdout or '', error.stderr or ''
                        out = out.decode(errors='replace') if isinstance(out, bytes) else out
                        err = err.decode(errors='replace') if isinstance(err, bytes) else err
                    stdout_file = f'probe-{rep}-{side}.stdout'
                    stderr_file = f'probe-{rep}-{side}.stderr'
                    (run_dir / stdout_file).write_text(out)
                    (run_dir / stderr_file).write_text(err)
                    record = {'rep': rep, 'side': side, 'rc': rc, 'wall': round(time.monotonic() - started, 4),
                              'result': last_json(out), 'stdout_tail': out[-2000:], 'stderr_tail': err[-2000:],
                              'stdout_file': stdout_file, 'stderr_file': stderr_file,
                              'load1': os.getloadavg()[0]}
                    records.append(record)
                    with open(run_dir / 'probe.jsonl', 'a') as f:
                        f.write(json.dumps(record) + '\n')
                    print(f"rep {rep} {side:6s} rc={rc} wall={record['wall']:.3f}s "
                          f"{json.dumps(record['result']) if record['result'] is not None else out.strip()[-300:]}", flush=True)
        finally:
            for side, path in paths.items():
                state = instance.stop(path)
                for log in path.glob('guest-*.log'):
                    (run_dir / f'guest-{side}.log').write_bytes(log.read_bytes())
                instance.remove(path)
            runs.write_json(run_dir / 'summary.json', {
                'schema': 'hd.probe/1', 'run_id': run_id, 'kind': 'probe', 'revision': rev,
                'program': str(a.program), 'args': a.args, 'candidates': a.with_, 'kernel_args': a.kernel_arg,
                'ab': a.ab, 'cpus': cpus, 'bench_cpus': bench_cpus, 'artifacts': artifacts['ids'],
                'records': records})
    failed = [r for r in records if r['rc'] != 0]
    print(f'{len(records)} runs, {len(failed)} failed; raw: {run_dir}/probe.jsonl')
    return 1 if failed or not records else 0
