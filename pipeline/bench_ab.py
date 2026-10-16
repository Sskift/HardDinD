"""Within-run native/A/B pairs; inactive VMs are stopped at the host boundary."""
import contextlib
import hashlib
import os
import pathlib
import signal
import time

from pipeline import bench, config, instance, runs


def suspend(state, stopped):
    # virtiofsd dies with SIGSYS when continued under its seccomp filter. Leave
    # it waiting for requests: the stopped UML guest cannot submit new work.
    groups = [state['guest_pid'], state['network_pid']]
    if os.getpgrp() in groups:
        raise RuntimeError('cannot suspend the benchmark process group')
    for pid in groups:
        try:
            os.killpg(pid, signal.SIGSTOP if stopped else signal.SIGCONT)
        except ProcessLookupError:
            if stopped:
                raise
    if not stopped:
        return
    deadline = time.monotonic() + 5
    while True:
        active = []
        for group in groups:
            for pid in instance.group_members(group)[0]:
                try:
                    state_code = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0]
                    if state_code not in ('T', 't', 'Z'):
                        active.append(pid)
                except (FileNotFoundError, ProcessLookupError):
                    pass
        if not active:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'guest processes did not stop: {active}')
        for group in groups:
            os.killpg(group, signal.SIGSTOP)
        time.sleep(.01)


def order(pair, native=True):
    sides = ['native', 'guest', 'guest_b'] if native else ['guest', 'guest_b']
    offset = pair % len(sides)
    return sides[offset:] + sides[:offset]


@contextlib.contextmanager
def samples(runner, name, side, switch):
    switch(side)
    with runner.samples(name) as functions:
        try:
            yield functions
        finally:
            switch(side)  # Context cleanup may use this guest's Docker API.


def measure(ctx, name, metric, functions, switch, host_load, pairs):
    native = 'native' in functions
    for pair in [None, *range(pairs)]:
        for side in order(0 if pair is None else pair, native):
            switch(side)
            value = bench.sample(ctx, name, metric, functions[side], side, pair)
            if pair is not None:
                metric[side].append(value)
        if pair is not None:
            host_load.append([time.time(), os.getloadavg()[0]])


def workloads(contexts, switch):
    ctx = contexts['guest']
    pairs = 2 if ctx.quick else 5
    result = {'schema': 'hd.bench/1', 'ab': True, 'cpus': ctx.bench_cpus, 'pairs': pairs,
              'isolation': 'inactive guest and network process groups SIGSTOP; verified stopped; virtiofsd remains idle',
              'host_load': [], 'metrics': {}}
    for name in bench.METRICS:
        if ctx.selected(name):
            unit, higher = bench.metric_info(name)
            result['metrics'][name] = {'unit': unit, 'higher_is_better': higher,
                                       'native': [], 'guest': [], 'guest_b': [], 'error': None}
    if not result['metrics']:
        return result
    runners, details = {}, {}
    try:
        for side, context in contexts.items():
            switch(side)
            runners[side] = bench.Workloads(context)
            details[side] = runners[side].prepare()
        for name, metric in result['metrics'].items():
            metric['detail'] = {'sides': {side: dict(detail) for side, detail in details.items()}}
            if name in ('disk.seq_read', 'disk.bind_read'):
                metric['detail']['read_policy'] = 'write+fsync then warm page-cache read with byte comparison; no cache drop'
            ctx.log(f'bench A/B {name}: warmup + {pairs} rotating triples')
            try:
                budget = 2 * bench.metric_budget(name, ctx.quick)
                for side, runner in runners.items():
                    switch(side)
                    runner.deadline = time.monotonic() + budget
                    detail = metric['detail']['sides'][side]
                    if name in ('disk.seq_write', 'disk.volume_write', 'app.untar'):
                        detail['durability'] = runner.durability()
                    if name in bench.BIND_METRICS:
                        detail['bind_visibility'] = runner.bind_visibility()
                    for check in ('durability', 'bind_visibility'):
                        if detail.get(check, {}).get('error'):
                            raise RuntimeError(side + ': ' + detail[check]['error'])
                with contextlib.ExitStack() as stack:
                    native, a = stack.enter_context(samples(runners['guest'], name, 'guest', switch))
                    _, b = stack.enter_context(samples(runners['guest_b'], name, 'guest_b', switch))
                    functions = {'guest': a, 'guest_b': b}
                    if native:
                        functions['native'] = native
                    measure(ctx, name, metric, functions, switch, result['host_load'], pairs)
                if name in ('disk.seq_write', 'disk.volume_write'):
                    for side, runner in runners.items():
                        if bench.statistics.median(metric[side]) > 2 * bench.statistics.median(metric['native']):
                            switch(side)
                            evidence = runner.write_diagnostics()
                            metric['detail']['sides'][side]['write_diagnostics'] = evidence
                            if evidence['error']:
                                raise RuntimeError(side + ': ' + evidence['error'])
            except Exception as error:
                metric['error'] = f'{type(error).__name__}: {error}'
                ctx.log(f'bench A/B {name}: {metric["error"]}')
    except Exception as error:
        for metric in result['metrics'].values():
            metric['error'] = f'prepare: {type(error).__name__}: {error}'
    finally:
        for side, runner in runners.items():
            try:
                switch(side)
                runner.close()
            except Exception as error:
                for metric in result['metrics'].values():
                    metric['error'] = metric['error'] or f'cleanup {side}: {error}'
    return result


def run(run_id, run_dir, artifacts, artifacts_b, args, cpus):
    contexts, states, paused = {}, {}, set()
    result = {}
    def switch(side):
        # Stop every other VM before waking the selected one; native stops both.
        for key, state in states.items():
            if key != side and key not in paused:
                paused.add(key)
                suspend(state, True)
        if side in paused:
            suspend(states[side], False)
            paused.remove(side)
    try:
        for side, label, built in [('guest', 'A', artifacts), ('guest_b', 'B', artifacts_b)]:
            switch('native')
            directory = run_dir / f'bench-{label}'; directory.mkdir()
            share = run_dir / f'share-bench-{label}'; share.mkdir()
            path = config.INSTANCES / ('b' + label.lower() + hashlib.sha256(run_id.encode()).hexdigest()[:10])
            opts = runs.instance_options(built, cpuset=cpus, shares=[share],
                kernel_args=args.kernel_arg + (args.ab if label == 'B' else []),
                share_opts=args.share_opt + (args.ab_share_opt if label == 'B' else []))
            contexts[side] = runs.Context(run_id + '-' + label, directory, path, built, opts,
                                          cpus, quick=args.quick, only=args.only, share=share)
            states[side] = instance.start(path, **opts)
            contexts[side].log(f'bench guest {label} ready on CPUs {cpus}')
        result = workloads(contexts, switch)
        return result
    finally:
        result['guest_states'] = {}
        errors = []
        for side, ctx in contexts.items():
            try:
                switch(side)
            except Exception as error:
                errors.append(f'switch {side}: {error}')
                # A failed switch must not leave the selected VM frozen during
                # shutdown, or prevent cleanup of either instance.
                if side in paused:
                    try:
                        suspend(states[side], False)
                    except Exception as resume_error:
                        errors.append(f'resume {side}: {resume_error}')
            try:
                state = instance.stop(ctx.instance)
                result['guest_states'][side] = state
                label = 'A' if side == 'guest' else 'B'
                instance.archive_logs(ctx.instance, run_dir / f'instance-bench-{label}')
                if not args.keep:
                    instance.remove(ctx.instance)
            except Exception as error:
                errors.append(f'{side}: {error}')
            finally:
                states.pop(side, None)
                paused.discard(side)
        if errors:
            raise RuntimeError('; '.join(errors))
