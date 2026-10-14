#!/usr/bin/env python3
"""Real Docker published-port lifecycle checks; never full product acceptance."""
import argparse
import concurrent.futures
import datetime
import fcntl
import ipaddress
import io
import json
import os
import pathlib
import shlex
import socket
import signal
import subprocess
import sys
import tarfile
import threading
import time
import harddind
from guest_cleanup import cleanup_group


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--kernel', type=pathlib.Path, required=True)
    p.add_argument('--disk', type=pathlib.Path, required=True)
    p.add_argument('--name', required=True)
    a = p.parse_args()
    if not a.name or len(a.name) > 32 or not all(c.isalnum() or c in '-_' for c in a.name): p.error('invalid name')
    work = a.work.resolve(); repo = pathlib.Path(__file__).resolve().parents[1]
    instance = work/'instances'/a.name; out = work/'results'; out.mkdir(exist_ok=True)
    result = out/(a.name+'.json'); logfile = out/(a.name+'.log'); archive = out/(a.name+'-rootfs.tar')
    if any(f.exists() for f in [instance, result, logfile, archive]): p.error('refusing existing evidence or instance')
    os.sched_setaffinity(0, [0, 1, 2, 3])
    base = [sys.executable, str(repo/'scripts/harddind.py'), str(work), a.name]
    start = base+['start', '--kernel', str(a.kernel.resolve()), '--disk', str(a.disk.resolve()), '--dns', '1.1.1.1', '--publish-ports']
    names = ['daemon_driver', 'fixed_tcp', 'fixed_udp', 'stop_restart_release', 'remove_release',
             'guest_alias_lifetime', 'guest_alias_failed_start',
             'outer_conflict_rollback', 'user_bridge_conflict_rollback', 'multiport_rollback',
             'ipv6_tcp_udp_publish', 'distinct_host_ips_same_port',
             'wildcard_specific_conflict_v4', 'wildcard_specific_conflict_v6',
             'manual_mapping_ownership', 'ephemeral_publish', 'port_range',
             'publish_all', 'concurrent_publish', 'daemon_restart_restore', 'clean_shutdown']
    checks = dict.fromkeys(names, 'not_tested')
    report = {'kind': 'docker_published_ports_subset', 'checks': checks, 'full_acceptance': False,
              'performance_acceptance': False, 'commands': [], 'address_snapshots': [],
              'ipv6_scope': 'IPv6 host publication to an IPv4 container on the default bridge through upstream docker-proxy',
              'not_tested': ['Swarm ingress publishing', 'outer-host non-loopback IPv6 reachability',
                             'abrupt host/guest failure recovery', 'GPU', 'full performance comparison'],
              'sha256': {str(f): harddind.sha(f) for f in [a.kernel, a.disk, work/'network-backend', work/'network-probe',
                  repo/'scripts/harddind.py', repo/'scripts/port_driver.py', pathlib.Path(__file__),
                  repo/'scripts/guest_cleanup.py', repo/'src/network_probe.c', repo/'src/network_backend.c',
                  repo/'guest/runtime-init', repo/'guest/runtime-daemon', repo/'sources.lock.json',
                  repo/'moby/patches/0003-publish-address-family.patch']}}
    program = (work/'network-probe').read_bytes()
    with tarfile.open(archive, 'w') as tar:
        entry = tarfile.TarInfo('netprobe'); entry.mode = 0o755; entry.size = len(program)
        tar.addfile(entry, io.BytesIO(program))
    report['rootfs_sha256'] = harddind.sha(archive)
    log = logfile.open('x'); log_lock = threading.Lock(); active = None
    command_index = 0
    image = 'harddind-port-probe:local'

    def run(args, expected=0, timeout=180):
        nonlocal command_index
        with log_lock:
            index = command_index; command_index += 1
        label = f'{a.name}-command-{index:03d}'
        issued = datetime.datetime.now(datetime.timezone.utc).isoformat()
        t0 = time.monotonic()
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=15)
            audit = cleanup_group(proc.pid)
        (out/(label+'.stdout')).write_bytes(stdout)
        (out/(label+'.stderr')).write_bytes(stderr)
        text = (stdout + stderr).decode(errors='replace')
        expected_ok = proc.returncode == 0 if expected == 0 else proc.returncode != 0
        row = {'index': index, 'command': args, 'issued_at': issued,
               'elapsed_s': time.monotonic()-t0, 'exit_code': proc.returncode,
               'expected': expected, 'expected_exit_satisfied': expected_ok,
               'timed_out': timed_out, 'cleanup': audit,
               'stdout': label+'.stdout', 'stderr': label+'.stderr'}
        with log_lock:
            report['commands'].append(row)
            log.write('$ '+shlex.join(args)+'\n'+text+f'\n[exit={proc.returncode}]\n'); log.flush()
        if not expected_ok or timed_out or not audit['clean_shutdown']:
            raise RuntimeError('unexpected command exit: '+shlex.join(args))
        return text
    def cli(*args, **kwargs): return run(base+['docker', *args], **kwargs)
    def begin(name):
        nonlocal active
        active = name; checks[name] = 'running'; print('starting '+name, flush=True)
    def passed(): checks[active] = 'pass'; print(active+' passed', flush=True)
    def free_port(udp=False, host='127.0.0.1'):
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
            if family == socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((host, 0)); return s.getsockname()[1]
    def echo(port, udp=False, host='127.0.0.1'):
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        until = time.monotonic()+15
        while True:
            try:
                with socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
                    s.settimeout(2); s.connect((host, port))
                    for size in ([1, 1200, 8000, 60000] if udp else [1, 65536]):
                        data = (bytes(range(256))*((size+255)//256))[:size]
                        s.sendall(data); received = bytearray()
                        while len(received) < size:
                            part = s.recv(65536 if udp else size-len(received))
                            if not part: raise RuntimeError('early EOF')
                            received.extend(part)
                            if udp: break
                        if received != data: raise RuntimeError('payload mismatch')
                return
            except OSError:
                if time.monotonic() > until: raise
                time.sleep(.1)
    def released(port, udp=False, host='127.0.0.1'):
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
            if family == socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if not udp: s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            if not udp: s.listen(1)
    def bindings(name): return json.loads(cli('inspect', '--format', '{{json .NetworkSettings.Ports}}', name))
    def published(name, proto='tcp', host='127.0.0.1'):
        rows = bindings(name)['7777/'+proto]
        if len(rows) != 1 or rows[0]['HostIp'] != host: raise RuntimeError('unexpected published address')
        port = int(rows[0]['HostPort']); echo(port, proto=='udp', host); return port
    def launch(name, publish, proto='tcp', extra=()):
        cli('run', '-d', '--name', name, *extra, '-p', publish, image, '/netprobe', proto, '7777', '0.0.0.0')
        return published(name, proto)
    def guest_addresses(label):
        raw = cli('run', '--rm', '--network', 'host', image, '/netprobe', 'addresses', 'vec0')
        addresses = set()
        for line in raw.splitlines():
            family, addr = line.split()
            parsed = ipaddress.ip_address(addr)
            assert str(parsed.version) == family
            addresses.add(str(parsed))
        required = {'10.0.2.15', '10.0.2.16', 'fd00::15', 'fd00::10'}
        assert required <= addresses, (label, addresses)
        report['address_snapshots'].append({'label': label, 'addresses': sorted(addresses)})
        return addresses
    def clean_state(state):
        return (state.get('phase') == 'stopped' and not state.get('forced_shutdown')
                and state.get('guest_cleanup', {}).get('clean_shutdown')
                and state.get('network_cleanup', {}).get('clean_shutdown'))
    lock_file = (work/'kernel-build.lock').open('a')
    fcntl.flock(lock_file, fcntl.LOCK_SH)
    try:
        begin('daemon_driver'); run(start)
        report['server'] = json.loads(cli('version', '--format', '{{json .Server}}'))
        assert report['server']['Version'] == '29.8.2-harddind.1'
        report['security_options'] = json.loads(cli('info', '--format', '{{json .SecurityOptions}}'))
        assert not any('rootless' in x for x in report['security_options'])
        assert any('seccomp' in x for x in report['security_options'])
        report['image_id'] = cli('import', str(archive), image).strip()
        passed()
        begin('guest_alias_lifetime')
        baseline = guest_addresses('baseline')
        previous_alias = None
        for cycle in range(2):
            at, au = free_port(host='127.0.0.2'), free_port(True, host='127.0.0.2')
            for proto, port in [('tcp', at), ('udp', au)]:
                cli('run', '-d', '--name', 'alias-'+proto, '-p', f'127.0.0.2:{port}:7777/{proto}',
                    image, '/netprobe', proto, '7777', '0.0.0.0')
                echo(port, proto=='udp', '127.0.0.2')
            live_alias = guest_addresses(f'cycle-{cycle}-both') - baseline
            assert len(live_alias) == 1 and ipaddress.ip_address(next(iter(live_alias))).version == 4
            if previous_alias is not None: assert live_alias == previous_alias
            cli('rm', '-f', 'alias-tcp'); released(at, host='127.0.0.2')
            assert guest_addresses(f'cycle-{cycle}-udp-only') == baseline | live_alias
            echo(au, True, '127.0.0.2')
            cli('rm', '-f', 'alias-udp'); released(au, True, '127.0.0.2')
            assert guest_addresses(f'cycle-{cycle}-released') == baseline
            previous_alias = live_alias
        passed()
        begin('guest_alias_failed_start')
        with socket.socket() as holder:
            holder.bind(('127.0.0.3', 0)); holder.listen(1)
            occupied = holder.getsockname()[1]
            cli('create', '--name', 'alias-failed', '-p', f'127.0.0.3:{occupied}:7777',
                image, '/netprobe', 'tcp', '7777', '0.0.0.0')
            error = cli('start', 'alias-failed', expected='failure')
            assert 'host port unavailable' in error, error
            assert cli('inspect', '-f', '{{.State.Running}}', 'alias-failed').strip() == 'false'
            assert guest_addresses('failed-start-rollback') == baseline
        cli('rm', 'alias-failed'); passed()
        begin('fixed_tcp'); tcp = free_port()
        assert launch('tcp', f'127.0.0.1:{tcp}:7777', extra=('--restart=always',)) == tcp
        assert cli('port', 'tcp', '7777/tcp').strip() == f'127.0.0.1:{tcp}'
        passed()
        begin('fixed_udp'); udp = free_port(True)
        assert launch('udp', f'127.0.0.1:{udp}:7777/udp', 'udp') == udp
        passed()
        begin('stop_restart_release')
        cli('stop', 'tcp', 'udp'); released(tcp); released(udp, True)
        cli('start', 'tcp', 'udp'); echo(tcp); echo(udp, True); passed()
        begin('remove_release'); cli('rm', '-f', 'udp'); released(udp, True); passed()
        begin('outer_conflict_rollback')
        with socket.socket() as holder:
            holder.bind(('127.0.0.1', 0)); holder.listen(1); occupied = holder.getsockname()[1]
            cli('create', '--name', 'conflict', '-p', f'127.0.0.1:{occupied}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
            error = cli('start', 'conflict', expected='failure')
            assert 'host port unavailable' in error, error
            assert cli('inspect', '--format', '{{.State.Running}}', 'conflict').strip() == 'false'
            assert 'bridge' in json.loads(cli('inspect', '--format', '{{json .NetworkSettings.Networks}}', 'conflict'))
        cli('start', 'conflict'); assert published('conflict') == occupied
        cli('rm', '-f', 'conflict'); released(occupied); passed()
        begin('user_bridge_conflict_rollback')
        cli('network', 'create', '--opt', 'com.docker.network.bridge.host_binding_ipv4=127.0.0.1', 'loopback')
        with socket.socket() as holder:
            holder.bind(('127.0.0.1', 0)); holder.listen(1); occupied = holder.getsockname()[1]
            cli('create', '--name', 'user-conflict', '--network', 'loopback', '--network-alias', 'retained-alias',
                '-p', f'127.0.0.1:{occupied}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
            cli('start', 'user-conflict', expected='failure')
            networks = json.loads(cli('inspect', '--format', '{{json .NetworkSettings.Networks}}', 'user-conflict'))
            assert 'retained-alias' in networks['loopback']['Aliases']
        cli('start', 'user-conflict'); assert published('user-conflict') == occupied
        cli('rm', '-f', 'user-conflict'); released(occupied); passed()
        begin('multiport_rollback'); first_port = free_port()
        with socket.socket() as holder:
            holder.bind(('127.0.0.1', 0)); holder.listen(1); occupied = holder.getsockname()[1]
            cli('create', '--name', 'multi-conflict', '-p', f'127.0.0.1:{first_port}:7777',
                '-p', f'127.0.0.1:{occupied}:8888', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
            cli('start', 'multi-conflict', expected='failure'); released(first_port)
        cli('start', 'multi-conflict'); assert published('multi-conflict') == first_port
        assert int(bindings('multi-conflict')['8888/tcp'][0]['HostPort']) == occupied
        cli('rm', '-f', 'multi-conflict'); released(first_port); released(occupied); passed()
        begin('ipv6_tcp_udp_publish')
        v6_baseline = guest_addresses('before-v6-publication')
        v6t = free_port(host='::1'); v6u = free_port(udp=True, host='::1')
        cli('run', '-d', '--name', 'v6tcp', '-p', f'[::1]:{v6t}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        cli('run', '-d', '--name', 'v6udp', '-p', f'[::1]:{v6u}:7777/udp', image, '/netprobe', 'udp', '7777', '0.0.0.0')
        echo(v6t, host='::1'); echo(v6u, udp=True, host='::1')
        assert published('v6tcp', host='::1') == v6t
        assert published('v6udp', 'udp', host='::1') == v6u
        v6_alias = guest_addresses('v6-tcp-and-udp') - v6_baseline
        assert len(v6_alias) == 1 and ipaddress.ip_address(next(iter(v6_alias))).version == 6
        cli('stop', 'v6tcp'); released(v6t, host='::1')
        assert guest_addresses('v6-udp-only') == v6_baseline | v6_alias
        cli('start', 'v6tcp'); echo(v6t, host='::1')
        cli('rm', '-f', 'v6tcp', 'v6udp')
        released(v6t, host='::1'); released(v6u, udp=True, host='::1')
        assert guest_addresses('v6-released') == v6_baseline
        passed()

        def expect_start_fail(name):
            error = cli('start', name, expected='failure')
            assert 'host port unavailable' in error, error
            assert cli('inspect', '-f', '{{.State.Running}}', name).strip() == 'false'

        begin('distinct_host_ips_same_port')
        dport = free_port()
        cli('run', '-d', '--name', 'ip-one', '-p', f'127.0.0.1:{dport}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        cli('run', '-d', '--name', 'ip-two', '-p', f'127.0.0.2:{dport}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        echo(dport, host='127.0.0.1'); echo(dport, host='127.0.0.2')
        assert published('ip-one') == dport
        assert published('ip-two', host='127.0.0.2') == dport
        cli('rm', '-f', 'ip-two')
        # The 127.0.0.2 socket and its guest alias must be gone; the surviving
        # mapping on 127.0.0.1 keeps serving.
        echo(dport, host='127.0.0.1')
        cli('rm', '-f', 'ip-one'); released(dport, host='127.0.0.1')
        released(dport, host='127.0.0.2')
        duport = free_port(True)
        for name, host in [('ip-udp-one', '127.0.0.1'), ('ip-udp-two', '127.0.0.2')]:
            cli('run', '-d', '--name', name, '-p', f'{host}:{duport}:7777/udp',
                image, '/netprobe', 'udp', '7777', '0.0.0.0')
            assert published(name, 'udp', host) == duport
        cli('rm', '-f', 'ip-udp-two'); released(duport, True, '127.0.0.2')
        echo(duport, True, '127.0.0.1')
        cli('rm', '-f', 'ip-udp-one'); released(duport, True, '127.0.0.1')
        passed()

        begin('wildcard_specific_conflict_v4')
        w1, w2 = free_port(), free_port()
        # Specific bind first, wildcard must fail and roll back atomically.
        cli('run', '-d', '--name', 'v4-specific', '-p', f'127.0.0.1:{w1}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        cli('create', '--name', 'v4-wild', '-p', f'{w1}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        expect_start_fail('v4-wild')
        echo(w1)  # the existing container still serves
        cli('rm', '-f', 'v4-wild', 'v4-specific')
        # Wildcard first, specific must fail in the other order as well.
        cli('run', '-d', '--name', 'v4-wild2', '-p', f'{w2}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        cli('create', '--name', 'v4-specific2', '-p', f'127.0.0.1:{w2}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        expect_start_fail('v4-specific2')
        cli('rm', '-f', 'v4-specific2', 'v4-wild2')
        released(w1); released(w2)
        passed()

        begin('wildcard_specific_conflict_v6')
        x1, x2 = free_port(host='::1'), free_port(host='::1')
        cli('run', '-d', '--name', 'v6-specific', '-p', f'[::1]:{x1}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        cli('create', '--name', 'v6-wild', '-p', f'[::]:{x1}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        expect_start_fail('v6-wild')
        echo(x1, host='::1')
        cli('rm', '-f', 'v6-wild', 'v6-specific')
        cli('run', '-d', '--name', 'v6-wild2', '-p', f'[::]:{x2}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        cli('create', '--name', 'v6-specific2', '-p', f'[::1]:{x2}:7777',
            image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        expect_start_fail('v6-specific2')
        cli('rm', '-f', 'v6-specific2', 'v6-wild2')
        released(x1, host='::1'); released(x2, host='::1')
        passed()

        begin('manual_mapping_ownership'); manual = free_port()
        harddind.control(instance, f'add tcp 127.0.0.1 {manual} {manual}')
        cli('create', '--name', 'manual-conflict', '-p', f'127.0.0.1:{manual}:7777', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        error = cli('start', 'manual-conflict', expected='failure'); assert 'host port already allocated' in error, error
        before = harddind.control(instance, 'list'); cli('rm', 'manual-conflict')
        assert harddind.control(instance, 'list') == before
        harddind.control(instance, f'del tcp 127.0.0.1 {manual} {manual}'); released(manual); passed()
        begin('ephemeral_publish'); ephemeral = launch('ephemeral', '127.0.0.1::7777')
        cli('rm', '-f', 'ephemeral'); released(ephemeral); passed()
        begin('port_range'); lower = free_port()
        lower = min(lower, 65533)
        ranged = launch('range', f'127.0.0.1:{lower}-{lower+2}:7777')
        assert lower <= ranged <= lower+2
        cli('rm', '-f', 'range'); released(ranged); passed()
        begin('publish_all')
        cli('run', '-d', '--name', 'all', '--network', 'loopback', '--expose', '7777', '-P', image, '/netprobe', 'tcp', '7777', '0.0.0.0')
        all_port = published('all'); cli('rm', '-f', 'all'); released(all_port); passed()
        begin('concurrent_publish')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ports = list(pool.map(lambda i: launch('parallel-'+str(i), '127.0.0.1::7777'), range(4)))
        assert len(set(ports)) == 4
        cli('rm', '-f', *('parallel-'+str(i) for i in range(4)))
        for port in ports: released(port)
        passed()
        begin('daemon_restart_restore')
        run(base+['stop'], timeout=70); report['first_run'] = harddind.read_state(instance)
        assert clean_state(report['first_run']); released(tcp)
        run(start); echo(tcp); assert published('tcp') == tcp
        cli('rm', '-f', 'tcp'); released(tcp)
        assert len(harddind.control(instance, 'list')) == 1
        passed()
    except BaseException as error:
        if active: checks[active] = 'fail'
        report['error'] = f'{type(error).__name__}: {error}'; print(report['error'], flush=True)
    finally:
        try:
            run(base+['stop'], timeout=70); report['final_run'] = harddind.read_state(instance)
            checks['clean_shutdown'] = 'pass' if clean_state(report['final_run']) else 'fail'
        except BaseException as error:
            checks['clean_shutdown'] = 'fail'; report['cleanup_error'] = str(error)
        report['sha256_after'] = {path: harddind.sha(pathlib.Path(path)) for path in report['sha256']}
        report['inputs_unchanged'] = report['sha256_after'] == report['sha256']
        report['subset_passed'] = (all(x == 'pass' for x in checks.values())
                                  and report['inputs_unchanged']
                                  and all(c['expected_exit_satisfied'] and not c['timed_out']
                                          and c['cleanup']['clean_shutdown'] for c in report['commands']))
        result.write_text(json.dumps(report, indent=2)+'\n'); log.close()
    print(json.dumps({'result': str(result), 'checks': checks, 'full_acceptance': False}, indent=2))
    return 0 if report['subset_passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
