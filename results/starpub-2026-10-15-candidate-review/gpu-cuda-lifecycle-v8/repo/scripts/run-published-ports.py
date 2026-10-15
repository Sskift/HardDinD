#!/usr/bin/env python3
"""Real Docker published-port lifecycle checks; never full product acceptance."""
import argparse
import concurrent.futures
import io
import json
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import tarfile
import threading
import time
import harddind


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
             'outer_conflict_rollback', 'user_bridge_conflict_rollback', 'multiport_rollback',
             'manual_mapping_ownership', 'ephemeral_publish', 'port_range',
             'publish_all', 'concurrent_publish', 'daemon_restart_restore', 'clean_shutdown']
    checks = dict.fromkeys(names, 'not_tested')
    report = {'kind': 'docker_published_ports_subset', 'checks': checks, 'full_acceptance': False,
              'not_tested': ['IPv6 publishing', 'different host IPs sharing one port', 'Swarm ingress publishing',
                             'abrupt host/guest failure recovery', 'GPU', 'full performance comparison'],
              'sha256': {str(f): harddind.sha(f) for f in [a.kernel, a.disk, work/'network-backend', work/'network-probe',
                  repo/'scripts/harddind.py', repo/'scripts/port_driver.py', pathlib.Path(__file__)]}}
    program = (work/'network-probe').read_bytes()
    with tarfile.open(archive, 'w') as tar:
        entry = tarfile.TarInfo('netprobe'); entry.mode = 0o755; entry.size = len(program)
        tar.addfile(entry, io.BytesIO(program))
    report['rootfs_sha256'] = harddind.sha(archive)
    log = logfile.open('x'); log_lock = threading.Lock(); active = None
    image = 'harddind-port-probe:local'

    def run(args, expected=0, timeout=180):
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        text = r.stdout.decode(errors='replace')
        with log_lock:
            log.write('$ '+shlex.join(args)+'\n'+text+f'\n[exit={r.returncode}]\n'); log.flush()
        if (expected == 0 and r.returncode) or (expected == 'failure' and not r.returncode):
            raise RuntimeError('unexpected command exit: '+shlex.join(args))
        return text
    def cli(*args, **kwargs): return run(base+['docker', *args], **kwargs)
    def begin(name):
        nonlocal active
        active = name; checks[name] = 'running'; print('starting '+name, flush=True)
    def passed(): checks[active] = 'pass'; print(active+' passed', flush=True)
    def free_port(udp=False):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0)); return s.getsockname()[1]
    def echo(port, udp=False):
        until = time.monotonic()+15
        while True:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
                    s.settimeout(2); s.connect(('127.0.0.1', port))
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
    def released(port, udp=False):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
            if not udp: s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', port))
            if not udp: s.listen(1)
    def bindings(name): return json.loads(cli('inspect', '--format', '{{json .NetworkSettings.Ports}}', name))
    def published(name, proto='tcp'):
        rows = bindings(name)['7777/'+proto]
        if len(rows) != 1 or rows[0]['HostIp'] != '127.0.0.1': raise RuntimeError('unexpected published address')
        port = int(rows[0]['HostPort']); echo(port, proto=='udp'); return port
    def launch(name, publish, proto='tcp', extra=()):
        cli('run', '-d', '--name', name, *extra, '-p', publish, image, '/netprobe', proto, '7777', '0.0.0.0')
        return published(name, proto)
    try:
        begin('daemon_driver'); run(start)
        report['server'] = json.loads(cli('version', '--format', '{{json .Server}}'))
        assert report['server']['Version'] == '29.8.2-harddind.1'
        report['security_options'] = json.loads(cli('info', '--format', '{{json .SecurityOptions}}'))
        assert not any('rootless' in x for x in report['security_options'])
        assert any('seccomp' in x for x in report['security_options'])
        report['image_id'] = cli('import', str(archive), image).strip()
        passed()
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
        assert report['first_run']['phase'] == 'stopped'; released(tcp)
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
            checks['clean_shutdown'] = 'pass' if report['final_run'].get('phase') == 'stopped' else 'fail'
        except BaseException as error:
            checks['clean_shutdown'] = 'fail'; report['cleanup_error'] = str(error)
        report['subset_passed'] = all(x == 'pass' for x in checks.values())
        result.write_text(json.dumps(report, indent=2)+'\n'); log.close()
    print(json.dumps({'result': str(result), 'checks': checks, 'full_acceptance': False}, indent=2))
    return 0 if report['subset_passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
