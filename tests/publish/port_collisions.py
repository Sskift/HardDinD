"""Force collisions outside UML, where the guest allocator cannot see them."""
import contextlib
import json
import socket

from pipeline.functional import HELPERS, echo, echo_server, published, released, require


def next_port(last, bounds, reserved):
    low, high = map(int, bounds.split())
    excluded = set()
    for item in reserved.strip().split(','):
        if item:
            ends = list(map(int, item.split('-')))
            excluded.update(range(ends[0], ends[-1] + 1))
    require(0 < low <= last <= high < 65536, 'invalid ephemeral range')
    for offset in range(1, high - low + 2):
        port = low + (last - low + offset) % (high - low + 1)
        if port not in excluded:
            return port
    raise ValueError('ephemeral range fully reserved')


def events(c):
    return [json.loads(line) for path in sorted(c.ctx.instance.glob('ports-*.jsonl'))
            for line in path.read_text().splitlines() if line]


def collision(rows, host, port, proto):
    return any(row.get('request', {}).get('method') == 'POST'
               and row['request'].get('body', {}).get('parentIP') == host
               and row['request']['body'].get('parentPort') == port
               and row['request']['body'].get('proto') == proto + '4'
               and row.get('response', {}).get('status') == 500
               and 'address already in use' in row['response'].get('body', {}).get('message', '').lower()
               for row in rows)


def hold(stack, host, port, udp):
    sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM))
    sock.bind((host, port))
    if not udp:
        sock.listen(1)
    return sock


def run(c):
    bounds = c.output('cat', '/proc/sys/net/ipv4/ip_local_port_range', opts=('--network', 'host'))
    reserved = c.output('cat', '/proc/sys/net/ipv4/ip_local_reserved_ports', opts=('--network', 'host'))
    for udp in (False, True):
        proto = 'udp' if udp else 'tcp'
        suffix = '/udp' if udp else ''
        prime = echo_server(c, '127.0.0.1::7777' + suffix, udp)
        port = next_port(published(c, prime, udp), bounds, reserved)
        with contextlib.ExitStack() as stack:
            hold(stack, '127.0.0.1', port, udp)
            before = len(events(c))
            automatic = echo_server(c, '127.0.0.1::7777' + suffix, udp)
            selected = published(c, automatic, udp)
            require(selected != port, 'automatic publish reused occupied outer port')
            require(collision(events(c)[before:], '127.0.0.1', port, proto), 'forced automatic collision was not exercised')
            echo(c, selected, udp)
            failed = c.container('/network_probe', proto, '7777', '0.0.0.0', image=HELPERS,
                                 create=True, opts=('-p', f'127.0.0.1:{port}:7777' + suffix))
            error = c.d('start', failed, expected='failure')
            require('address already in use' in error.lower(), error)
            require(not c.inspect(failed)['State']['Running'], 'fixed conflict started container')
        # Retry the exact failed fixed mapping after releasing the outer holder.
        c.d('start', failed)
        echo(c, port, udp)
        c.d('rm', '-f', failed, automatic, prime)
        released(c, port, udp)

        # A fresh two-port range, with a collision on the second host address,
        # exercises rollback of the first address's already-created forward.
        with contextlib.ExitStack() as stack:
            for _ in range(32):
                with contextlib.ExitStack() as trial:
                    blocker = hold(trial, '127.0.0.2', 0, udp)
                    low = blocker.getsockname()[1]
                    if low == 65535:
                        continue
                    try:
                        for host in ('127.0.0.1', '127.0.0.2'):
                            probe = hold(trial, host, low + 1, udp)
                            probe.close()
                        probe = hold(trial, '127.0.0.1', low, udp)
                        probe.close()
                    except OSError:
                        continue
                    stack.enter_context(trial.pop_all())
                    break
            else:
                raise RuntimeError('no free two-port range')
            before = len(events(c))
            name = echo_server(c, f'127.0.0.1:{low}-{low+1}:7777' + suffix, udp,
                               opts=('-p', f'127.0.0.2:{low}-{low+1}:7777' + suffix))
            require(collision(events(c)[before:], '127.0.0.2', low, proto), 'forced range collision was not exercised')
            for host in ('127.0.0.1', '127.0.0.2'):
                require(published(c, name, udp, host) == low + 1, 'range retry chose wrong port')
                echo(c, low + 1, udp, host)
            released(c, low, udp, '127.0.0.1')
            c.d('rm', '-f', name)
            for host in ('127.0.0.1', '127.0.0.2'):
                released(c, low + 1, udp, host)
