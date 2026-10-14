#!/usr/bin/env python3
"""Standalone, repeatable Docker Compose acceptance against the in-guest daemon.

The pinned outer Docker CLI (WORK/client, musl 29.8.2) drives the *real*
dockerd inside the UML guest over the loopback API relay. Every Compose object
(containers, user network, named volume, BuildKit build) is created inside the
guest; this script only stages the pinned Compose plugin and issues commands.

Missing Compose support is a nonzero, not-passing result: when the pinned
plugin is absent the report records every check as not_tested without booting
the guest. The base image must already exist in the daemon (digest-pinned);
--pull-pinned is an explicit opt-in that may pull only the lock-pinned digest,
never a mutable tag.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
import harddind
from guest_cleanup import cleanup_group

PROJECT = 'harddind-compose'
NETWORK = PROJECT + '_default'
VOLUME = PROJECT + '_data'
BUILT_IMAGE = 'harddind-compose-built:local'
BUILT_MARKER = 'harddind-compose-built'
BUILTIN_NETWORKS = {'bridge', 'host', 'none'}

# Ordered acceptance phases. pinned_image_pull is inserted before
# pinned_image_local only when the operator explicitly opts in.
BASE_CHECKS = ['compose_plugin', 'clean_start', 'pinned_image_local',
               'compose_config', 'compose_build', 'compose_up', 'health_dependency',
               'compose_exec', 'service_dns', 'volume_recreate', 'scale_workers',
               'compose_logs', 'down_cleanup', 'daemon_leftovers', 'clean_shutdown']

NOT_TESTED_SCOPE = ['compose CLI installed inside the guest (not in the pinned Alpine package set)',
                    'outer path bind mounts (shared-directory semantics still experimental)',
                    'registry push/login, bake, watch, secrets/configs, profiles, Swarm stacks, rollouts',
                    'external network and performance']


def is_digest_ref(ref):
    """A pinned reference has an @sha256:<64 hex> suffix; mutable tags are refused."""
    repo, at, digest = (ref or '').partition('@')
    if not at or not repo:
        return False
    name, colon, value = digest.partition(':')
    return (name == 'sha256' and colon == ':' and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def version_matches(reported, locked):
    reported = (reported or '').strip().lstrip('v')
    locked = (locked or '').strip().lstrip('v')
    return bool(reported) and reported == locked


def parse_time(value):
    # Docker emits nanoseconds; Python 3.10 accepts at most microseconds.
    return datetime.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', value.replace('Z', '+00:00')))


class PhaseSkipped(Exception):
    pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path)
    p.add_argument('--name', required=True)
    p.add_argument('--dns', default='10.0.2.3')
    p.add_argument('--mtu', type=int, default=1500)
    p.add_argument('--image', help='digest-pinned local image; defaults to sources.lock.json')
    p.add_argument('--compose-plugin', type=pathlib.Path, help='explicit pinned plugin binary')
    p.add_argument('--memfd-ram', action='store_true')
    p.add_argument('--memfd-stub', action='store_true')
    p.add_argument('--pull-pinned', action='store_true', help='allow pulling exactly the lock-pinned digest image')
    a = p.parse_args()
    if not a.name or len(a.name) > 32 or not all(c.isalnum() or c in '-_' for c in a.name):
        p.error('invalid name')
    work = a.work.resolve(); repo = pathlib.Path(__file__).resolve().parents[1]
    lock = json.loads((repo / 'sources.lock.json').read_text())
    plugin_lock = lock['docker_compose']
    image = a.image or lock['network_test_image']['reference']
    names = list(BASE_CHECKS)
    if a.pull_pinned:
        names.insert(names.index('pinned_image_local'), 'pinned_image_pull')
    checks = dict.fromkeys(names, 'not_tested')
    instance = work / 'instances' / a.name
    out = work / 'results'; out.mkdir(exist_ok=True)
    report_path = out / (a.name + '.json'); log_path = out / (a.name + '.log')
    if instance.exists() or report_path.exists() or log_path.exists():
        p.error('refusing existing instance or evidence')
    os.sched_setaffinity(0, [0, 1, 2, 3])
    fixture = repo / 'tests/compose'
    report = {'kind': 'compose_acceptance_subset', 'checks': checks, 'full_acceptance': False,
              'not_tested': list(NOT_TESTED_SCOPE), 'project': PROJECT,
              'compose': {'locked_version': plugin_lock['version'], 'lock_sha256': plugin_lock['sha256']},
              'image': {'reference': image, 'pull_pinned': a.pull_pinned,
                        'matches_lock': image == lock['network_test_image']['reference']},
              'dns': a.dns, 'mtu': a.mtu,
              'host_cpu_affinity': sorted(os.sched_getaffinity(0)),
              'fixture_sha256': {str(q.relative_to(repo)): harddind.sha(q)
                                 for q in [fixture / 'compose.yaml', fixture / 'builder/Dockerfile']},
              'sources': {str(q.relative_to(repo)): harddind.sha(q)
                          for q in [pathlib.Path(__file__).resolve(), repo / 'scripts/harddind.py',
                                    repo / 'sources.lock.json']},
              'input_kernel_sha256': harddind.sha(a.kernel),
              'input_disk_sha256': harddind.sha(a.disk or work / 'runtime.ext4'),
              'command_cleanup': [], 'timings': {}}
    log = log_path.open('x')
    state = {}

    def log_run(args, env, rc, seconds, text):
        log.write('$ ' + shlex.join(args) + '\n' + text + f'\n[exit={rc} seconds={seconds:.3f}]\n'); log.flush()

    def write_report():
        report['checks'] = checks
        report['subset_passed'] = all(v == 'pass' for v in checks.values())
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    # ---- preflight: pinned plugin, resolved before booting the guest ----
    candidates = []
    if a.compose_plugin:
        candidates.append(a.compose_plugin.resolve())
    candidates += [work / 'client/cli-plugins/docker-compose']
    plugin_path = next((q for q in candidates if q.exists()), None)
    client_loader = work / 'client/ld-musl-x86_64.so.1'
    client_docker = work / 'client/docker'
    if plugin_path is None:
        report['compose']['status'] = 'not_tested'
        report['compose']['reason'] = ('pinned compose plugin not found; stage it with '
                                       'scripts/install-compose-client.sh or pass --compose-plugin')
        report['error'] = 'compose plugin missing'
        write_report(); log.close()
        print(json.dumps({'report': str(report_path), 'checks': checks, 'full_acceptance': False}, indent=2))
        return 1
    plugin_sha = harddind.sha(plugin_path)
    report['compose'].update({'path': str(plugin_path), 'sha256': plugin_sha,
                              'hash_matches_lock': plugin_sha == plugin_lock['sha256']})
    if plugin_sha != plugin_lock['sha256']:
        checks['compose_plugin'] = 'fail'
        report['error'] = 'compose plugin differs from sources.lock.json'
        write_report(); log.close()
        print(report['error'], file=sys.stderr)
        return 1
    if not client_loader.exists() or not client_docker.exists():
        checks['compose_plugin'] = 'fail'
        report['error'] = f'pinned docker client unavailable under {work}/client (make runtime-root)'
        write_report(); log.close()
        print(report['error'], file=sys.stderr)
        return 1
    tmp = work / 'tmp'; tmp.mkdir(exist_ok=True)
    version_dir = pathlib.Path(tempfile.mkdtemp(prefix='compose-version-', dir=tmp))
    (version_dir / 'config.json').write_text(
        json.dumps({'cliPluginsExtraDirs': list(dict.fromkeys([str(plugin_path.parent), str(work / 'client/cli-plugins')]))}) + '\n')
    version_env = {**os.environ, 'DOCKER_CONFIG': str(version_dir)}
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        version_env.pop(key, None)
    try:
        version_out = subprocess.run([str(client_loader), str(client_docker), 'compose', 'version', '--short'],
                                     env=version_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     timeout=30).stdout.decode(errors='replace').strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        checks['compose_plugin'] = 'fail'
        report['error'] = f'compose plugin failed to run: {error}'
        write_report(); log.close(); print(report['error'], file=sys.stderr); return 1
    finally:
        shutil.rmtree(version_dir)
    if not version_matches(version_out, plugin_lock['version']):
        checks['compose_plugin'] = 'fail'
        report['compose']['reported_version'] = version_out
        report['error'] = f"compose plugin reports {version_out!r}, lock pins {plugin_lock['version']}"
        write_report(); log.close(); print(report['error'], file=sys.stderr); return 1
    checks['compose_plugin'] = 'pass'
    report['compose']['reported_version'] = version_out

    base = [sys.executable, str(repo / 'scripts/harddind.py'), str(work), a.name]
    start = base + ['start', '--kernel', str(a.kernel.resolve()),
                    '--disk', str((a.disk or work / 'runtime.ext4').resolve()),
                    '--dns', a.dns, '--mtu', str(a.mtu)]
    if a.memfd_ram: start.append('--memfd-ram')
    if a.memfd_stub: start.append('--memfd-stub')
    compose_env = {}
    pulled_new_image = False
    teardown_done = False
    active = None
    skip = False

    def run(args, timeout=120, env=None, expected=0):
        started = time.monotonic()
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        finally:
            cleanup = cleanup_group(proc.pid)
            report['command_cleanup'].append({'pid': proc.pid, **cleanup})
        text = stdout.decode(errors='replace')
        errors = stderr.decode(errors='replace')
        log_run(args, env or {}, proc.returncode, time.monotonic() - started,
                text + ('\n[stderr]\n' + errors if errors else ''))
        if timed_out or not cleanup['clean_shutdown'] or proc.returncode != expected:
            raise RuntimeError(f'command exited {proc.returncode}, timeout={timed_out}: '
                               f'{shlex.join(args)}\n{(text + errors)[-1200:]}')
        return text

    def docker_env():
        port = harddind.read_state(instance).get('api_port')
        if not port:
            raise RuntimeError('instance api port missing')
        cfgdir = instance / 'compose-docker-config'; cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (cfgdir / 'config.json').write_text(
            json.dumps({'cliPluginsExtraDirs': list(dict.fromkeys([str(plugin_path.parent), str(work / 'client/cli-plugins')]))}) + '\n')
        env = {**os.environ, 'DOCKER_HOST': f'tcp://127.0.0.1:{port}',
               'DOCKER_CONFIG': str(cfgdir), 'COMPOSE_BASE_IMAGE': image,
               'COMPOSE_ANSI': '0', 'DOCKER_CLI_HINTS': 'false'}
        for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
            env.pop(key, None)
        return env

    def dkr(*args, **kwargs):
        return run([str(client_loader), str(client_docker), *args], env=docker_env(), **kwargs)

    def compose(*args, **kwargs):
        kwargs.setdefault('env', docker_env())
        return run([str(client_loader), str(client_docker), 'compose', '--project-name', PROJECT,
                    '-f', str(fixture / 'compose.yaml'), *args], **kwargs)

    def phase(name):
        nonlocal active, skip
        active = name
        if skip:
            checks[name] = 'not_tested'
            print(name + ' not_tested', flush=True)
            raise PhaseSkipped(name)
        checks[name] = 'running'; print('starting ' + name, flush=True)

    def passed():
        checks[active] = 'pass'; print(active + ' passed', flush=True)

    def fail(error):
        nonlocal skip
        checks[active] = 'fail'
        report['failed_phase'] = active
        report['error'] = f'{type(error).__name__}: {error}'
        print(report['error'], flush=True)
        skip = True

    def ps_rows(*services):
        text = compose('ps', '-a', '--format', 'json', *services)
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        for row in rows:
            row['_service'] = row.get('Service')
            if not row['_service']:
                name = row.get('Name', '').lstrip('/').removeprefix(PROJECT + '-')
                row['_service'] = name.rsplit('-', 1)[0]
        return rows

    def by_service(rows):
        result = {}
        for row in rows:
            result.setdefault(row['_service'], []).append(row)
        return result

    def snapshot():
        return {'containers': set(dkr('ps', '-aq').split()),
                'networks': set(dkr('network', 'ls', '--format', '{{.Name}}').split()) - BUILTIN_NETWORKS,
                'volumes': set(dkr('volume', 'ls', '--format', '{{.Name}}').split()),
                'images': set(dkr('image', 'ls', '-q', '--no-trunc').split())}

    try:
        # ---- instance lifecycle ----
        phase('clean_start')
        run(start, timeout=180)
        state = harddind.read_state(instance)
        compose_env = docker_env()
        residue = {
            'containers': dkr('ps', '-aq', '--filter', f'label=com.docker.compose.project={PROJECT}').strip(),
            'networks': dkr('network', 'ls', '--filter', f'name=^{NETWORK}$', '--format', '{{.Name}}').strip(),
            'volumes': dkr('volume', 'ls', '--filter', f'name=^{VOLUME}$', '--format', '{{.Name}}').strip()}
        assert not any(residue.values()), residue
        report['server'] = json.loads(dkr('version', '--format', '{{json .Server}}'))
        before = snapshot()
        report['before'] = {key: sorted(value) for key, value in before.items()}
        passed()

        # ---- pinned image present locally ----
        if a.pull_pinned:
            phase('pinned_image_pull')
            assert is_digest_ref(image), 'only digest-pinned images may be pulled'
            pull_out = dkr('pull', image, timeout=300)
            assert 'Digest: sha256:' in pull_out, pull_out[-400:]
            passed()
        phase('pinned_image_local')
        if not is_digest_ref(image):
            raise RuntimeError('image must be a digest-pinned reference (image@sha256:hex); mutable tags are refused')
        image_id = dkr('image', 'inspect', '--format', '{{.Id}}', image).strip()
        report['image']['id'] = image_id
        # Docker image ls abbreviates IDs; compare the same canonical representation.
        pulled_new_image = a.pull_pinned and image_id not in before['images']
        report['image']['busybox_applets'] = dkr('run', '--rm', image, 'busybox', '--list').split()
        assert {'nc', 'wget', 'nslookup'} <= set(report['image']['busybox_applets'])
        passed()

        # ---- static config rendered by the pinned plugin ----
        phase('compose_config')
        cfg = json.loads(compose('config', '--format', 'json'))
        services = cfg['services']
        assert set(services) == {'db', 'web', 'worker', 'builder'}
        for service_name, service in services.items():
            assert service.get('pull_policy') == 'never', (service_name, service.get('pull_policy'))
            assert not any(v.get('type') == 'bind' for v in service.get('volumes', []))
        for service_name in ('db', 'web', 'worker'):
            assert services[service_name]['image'] == image, (service_name, services[service_name]['image'])
        assert services['builder']['image'] == BUILT_IMAGE
        assert services['db']['volumes'][0]['type'] == 'volume'
        assert services['db']['volumes'][0]['source'] == 'data'
        assert services['db']['volumes'][0]['target'] == '/data'
        assert services['web']['depends_on']['db']['condition'] == 'service_healthy'
        assert services['db']['healthcheck']['test'] == ['CMD-SHELL', 'test -f /ready']
        build = services['builder']['build']
        # Normalized output omits pull when it is the default false; either way it must never be truthy.
        assert not build.get('pull') and build['args']['BASE_IMAGE'] == image
        assert 'data' in cfg['volumes']
        passed()

        # ---- real offline BuildKit build + run ----
        phase('compose_build')
        t = time.monotonic(); compose('build', '--no-cache', 'builder', timeout=300)
        report['timings']['build_seconds'] = time.monotonic() - t
        built = compose('run', '--rm', '-T', 'builder').strip()
        assert built == BUILT_MARKER, built
        passed()

        # ---- up; the health gate delays web ----
        phase('compose_up')
        t = time.monotonic()
        compose('up', '-d', '--pull', 'never', 'db', 'web', 'worker', timeout=180)
        report['timings']['up_seconds'] = time.monotonic() - t
        rows = by_service(ps_rows('db', 'web', 'worker'))
        assert set(rows) == {'db', 'web', 'worker'}
        assert all(r['State'] == 'running' for r in sum(rows.values(), []))
        passed()

        # ---- depends_on service_healthy really blocked web ----
        phase('health_dependency')
        ids = {name: rows[name][0]['ID'] for name in rows}
        db_state = json.loads(dkr('inspect', '--format', '{{json .State}}', ids['db']))
        web_state = json.loads(dkr('inspect', '--format', '{{json .State}}', ids['web']))
        assert db_state['Health']['Status'] == 'healthy', db_state['Health']['Status']
        healthy = [entry for entry in db_state['Health']['Log'] if entry['ExitCode'] == 0]
        assert healthy, db_state['Health']
        t_db = parse_time(db_state['StartedAt']); t_web = parse_time(web_state['StartedAt'])
        # Health.Log is a rotating five-entry window. Use the transition event
        # so a slow CLI cannot make a valid dependency look too early.
        events = dkr('events', '--since', db_state['StartedAt'], '--until',
                     datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     '--filter', 'container=' + ids['db'], '--filter', 'event=health_status',
                     '--format', '{{json .}}')
        health_events = [json.loads(line) for line in events.splitlines() if line.strip()]
        report['health_events'] = health_events
        ready = [event for event in health_events if event.get('Action') == 'health_status: healthy']
        assert ready, health_events
        t_healthy = datetime.datetime.fromtimestamp(min(event['timeNano'] for event in ready) / 1e9, datetime.timezone.utc)
        assert t_web >= t_healthy - datetime.timedelta(microseconds=1), (t_web, t_healthy)
        delay = (t_web - t_db).total_seconds()
        report['health_gate_delay_seconds'] = delay
        assert delay >= 4.0, delay
        passed()

        # ---- exec ----
        phase('compose_exec')
        assert compose('exec', '-T', 'web', 'cat', '/www/index.html').strip() == 'harddind-web-marker'
        passed()

        # ---- service DNS with a real HTTP request ----
        phase('service_dns')
        answer = compose('exec', '-T', 'web', 'wget', '-qO-', '-T', '5', 'http://db:8080/').strip()
        assert answer == 'harddind-db-marker', answer
        passed()

        # ---- named volume survives force-recreate ----
        phase('volume_recreate')
        marker = 'harddind-persist-' + uuid.uuid4().hex[:8]
        stored = compose('exec', '-T', 'db', 'sh', '-c',
                         f'echo {marker} > /data/marker; cat /data/marker').strip()
        assert stored == marker
        before_ids = {name: [r['ID'] for r in group] for name, group in
                      by_service(ps_rows('db', 'web', 'worker')).items()}
        compose('up', '-d', '--pull', 'never', '--force-recreate', 'db', 'web', 'worker', timeout=180)
        after_groups = by_service(ps_rows('db', 'web', 'worker'))
        assert set(after_groups) == {'db', 'web', 'worker'}
        for name, group in after_groups.items():
            assert all(r['ID'] not in before_ids[name] for r in group), (name, 'container was not recreated')
        kept = compose('exec', '-T', 'db', 'cat', '/data/marker').strip()
        assert kept == marker, kept
        assert dkr('volume', 'inspect', '--format', '{{.Name}}', VOLUME).strip() == VOLUME
        passed()

        # ---- scale to 3 tasks and observe DNS round-robin ----
        phase('scale_workers')
        compose('up', '-d', '--pull', 'never', '--scale', 'worker=3', 'worker', timeout=180)
        workers = by_service(ps_rows('worker'))['worker']
        worker_ids = {r['ID'] for r in workers}
        assert len(workers) == 3 and len(worker_ids) == 3
        assert all(r['State'] == 'running' for r in workers)
        endpoints = {}
        for worker_id in sorted(worker_ids):
            item = json.loads(dkr('inspect', worker_id))[0]
            endpoints[item['NetworkSettings']['Networks'][NETWORK]['IPAddress']] = item['Config']['Hostname']
        answer = compose('exec', '-T', 'web', 'nslookup', 'worker')
        addresses = set(re.findall(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b', answer))
        assert set(endpoints) <= addresses, (endpoints, answer)
        seen = {}
        for address, hostname in endpoints.items():
            response = compose('exec', '-T', 'web', 'wget', '-qO-', '-T', '5',
                               f'http://{address}:8080/').strip()
            assert response == hostname, (address, response, hostname)
            seen[address] = response
        report['scale_dns_addresses'] = sorted(addresses)
        report['scale_seen_backends'] = seen
        passed()

        # ---- aggregated logs ----
        phase('compose_logs')
        logs = compose('logs', '--no-color', 'db', 'web', 'worker')
        for line in ('harddind-compose-db', 'harddind-compose-web', 'harddind-compose-worker'):
            assert line in logs, line
        passed()

        # ---- down removes project objects ----
        phase('down_cleanup')
        compose('down', '--volumes', '--remove-orphans', timeout=120)
        assert not dkr('ps', '-aq', '--filter', f'label=com.docker.compose.project={PROJECT}').strip()
        assert not dkr('network', 'ls', '--filter', f'name=^{NETWORK}$', '--format', '{{.Name}}').strip()
        assert not dkr('volume', 'ls', '--filter', f'name=^{VOLUME}$', '--format', '{{.Name}}').strip()
        dkr('rmi', BUILT_IMAGE)
        if pulled_new_image:
            dkr('rmi', image)
        teardown_done = True
        passed()

        # ---- daemon-wide leftovers versus the pre-run snapshot ----
        phase('daemon_leftovers')
        after = snapshot()
        diffs = {key: sorted(after[key] - before[key]) for key in before}
        report['leftovers'] = diffs
        assert not any(diffs.values()), diffs
        passed()
    except PhaseSkipped:
        pass
    except BaseException as error:
        if active is not None and checks.get(active) == 'running':
            fail(error)
    finally:
        # Best-effort project teardown after any aborted phase.
        if state.get('api_port') and not teardown_done:
            try:
                report['failure_containers'] = dkr('ps', '-a', '--no-trunc')
                report['failure_logs'] = compose('logs', '--no-color')
            except BaseException as error:
                report['failure_diagnostics_error'] = str(error)
            try:
                run([str(client_loader), str(client_docker), 'compose', '--project-name', PROJECT,
                     '-f', str(fixture / 'compose.yaml'), 'down', '--volumes', '--remove-orphans'],
                    timeout=60, env=compose_env or docker_env())
                run([str(client_loader), str(client_docker), 'rmi', BUILT_IMAGE],
                    timeout=30, env=compose_env or docker_env(), expected=0)
            except BaseException as error:
                report['emergency_cleanup'] = f'{type(error).__name__}: {error}'
        try:
            run(base + ['stop'], timeout=70)
            final = harddind.read_state(instance)
            report['final_run'] = final
            checks['clean_shutdown'] = 'pass' if (
                final.get('phase') == 'stopped' and final.get('guest_cleanup', {}).get('clean_shutdown')
                and not final.get('forced_shutdown')) else 'fail'
            if checks['clean_shutdown'] != 'pass':
                report['error'] = report.get('error', 'guest did not shut down cleanly')
                skip = True
        except BaseException as error:
            checks['clean_shutdown'] = 'fail'
            report['cleanup_error'] = f'{type(error).__name__}: {error}'
        report['inputs_unchanged'] = (harddind.sha(a.kernel) == report['input_kernel_sha256']
                                      and harddind.sha(a.disk or work / 'runtime.ext4') == report['input_disk_sha256'])
        if not report['inputs_unchanged']:
            checks['clean_shutdown'] = 'fail'
        write_report(); log.close()
    print(json.dumps({'report': str(report_path), 'checks': checks, 'full_acceptance': False}, indent=2))
    return 0 if all(v == 'pass' for v in checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
