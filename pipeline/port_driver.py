"""RootlessKit port-control protocol for a rootful Docker guest.

Only the transport/API contract is reused. No host namespaces or rootless Docker
flags are used. A private inherited stream carries requests from the guest agent.
"""
import ipaddress
import json
import socket
import threading
import time

# Guest-side alias addresses configured by guest/runtime-init and mapped by
# Moby patch 0003. Keep these three definitions in lockstep. The host bind
# always uses the real requested parent IP; the alias only names the guest
# reservation/DNAT target so distinct parent IPs never collapse.
GUEST_IPV4 = ipaddress.IPv4Address('10.0.2.15')
WILDCARD_IPV4 = ipaddress.IPv4Address('10.0.2.16')          # parent 0.0.0.0
DYNAMIC_IPV4_LO = ipaddress.IPv4Address('10.0.2.17')
DYNAMIC_IPV4_HI = ipaddress.IPv4Address('10.0.2.254')
GUEST_IPV6 = ipaddress.IPv6Address('fd00::15')
WILDCARD_IPV6 = ipaddress.IPv6Address('fd00::10')           # parent ::
DYNAMIC_IPV6_NET = ipaddress.IPv6Network('fd00::100:0/112')
PROTOS = ('tcp4', 'tcp6', 'udp4', 'udp6')


def valid_child(proto, child):
    """The guest alias must be an address the static guest config actually
    owns on vec0; never an arbitrary address (no silent wildcard routing)."""
    if proto.endswith('4'):
        if not isinstance(child, ipaddress.IPv4Address): return False
        if child == GUEST_IPV4 or child == WILDCARD_IPV4: return True
        return DYNAMIC_IPV4_LO <= child <= DYNAMIC_IPV4_HI
    if not isinstance(child, ipaddress.IPv6Address) or child.ipv4_mapped is not None:
        return False
    if child in (GUEST_IPV6, WILDCARD_IPV6): return True
    return child in DYNAMIC_IPV6_NET and int(child) != int(DYNAMIC_IPV6_NET.network_address)


class PortDriver:
    def __init__(self, stream, control, log, dns):
        self.stream = stream
        self.control = control
        self.log = log
        self.dns = dns
        self.ports = {}
        self.next_id = 1
        self.info_seen = False
        self.error = None
        self.stopping = threading.Event()
        self.shutdown_expected = threading.Event()
        self.thread = threading.Thread(target=self._run, name='harddind-ports', daemon=True)

    def start(self):
        self.thread.start()

    def event(self, **fields):
        self.log.write(json.dumps({'time': time.time(), **fields}) + '\n')
        self.log.flush()

    @staticmethod
    def port_number(value):
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError('port must be an integer in 1..65535')
        return value

    def handle(self, method, path, body):
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError('invalid method or path')
        if method == 'GET' and path == '/v1/info':
            self.info_seen = True
            return 200, {'apiVersion': '1.1.2', 'version': 'harddind-port-v2',
                         'stateDir': '/run/harddind-port', 'childPID': 0,
                         'networkDriver': {'driver': 'harddind-uml', 'childIP': '10.0.2.15',
                                           'dns': self.dns.split(','), 'ipv6': True},
                         'portDriver': {'driver': 'harddind-slirp', 'protos': list(PROTOS),
                                        'disallowLoopbackChildIP': True}}
        if method == 'GET' and path == '/v1/ports':
            return 200, list(self.ports.values())
        if method == 'POST' and path == '/v1/ports':
            if not isinstance(body, dict) or set(body) - {'proto', 'parentIP', 'parentPort', 'childIP', 'childPort'}:
                raise ValueError('invalid port specification')
            proto = body.get('proto')
            if proto not in PROTOS:
                raise ValueError('supported protocols are tcp4, tcp6, udp4, udp6')
            want_v6 = proto.endswith('6')
            kind = ipaddress.IPv6Address if want_v6 else ipaddress.IPv4Address
            try:
                parent = kind(body.get('parentIP') or ('::' if want_v6 else '0.0.0.0'))
                child = kind(body.get('childIP') or (str(GUEST_IPV6) if want_v6 else str(GUEST_IPV4)))
            except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
                raise ValueError('parentIP/childIP family must match proto')
            if isinstance(parent, ipaddress.IPv6Address):
                if parent.ipv4_mapped is not None or parent.scope_id is not None:
                    raise ValueError('IPv4-mapped or scoped IPv6 parent addresses are not supported')
                if parent.is_link_local:
                    raise ValueError('link-local IPv6 parent addresses are not supported')
            if not valid_child(proto, child):
                raise ValueError('childIP is not a configured harddind guest alias')
            parent_port = self.port_number(body.get('parentPort'))
            child_port = self.port_number(body.get('childPort'))
            spec = {'proto': proto, 'parentIP': str(parent), 'parentPort': parent_port,
                    'childIP': str(child), 'childPort': child_port}
            # Strict creation prevents adopting an existing manual/API mapping.
            self.control(f'create {proto[:-1]} {parent} {parent_port} {child_port} {child}')
            status = {'id': self.next_id, 'spec': spec}
            self.ports[self.next_id] = status
            self.next_id += 1
            return 201, status
        if method == 'DELETE' and path.startswith('/v1/ports/'):
            suffix = path.removeprefix('/v1/ports/')
            if not suffix.isascii() or not suffix.isdigit(): raise ValueError('invalid port id')
            ident = int(suffix)
            if ident not in self.ports: return 404, {'message': 'port id not found'}
            spec = self.ports[ident]['spec']
            self.control(f'del {spec["proto"][:-1]} {spec["parentIP"]} {spec["parentPort"]} '
                         f'{spec["childPort"]} {spec["childIP"]}')
            del self.ports[ident]
            return 200, {}
        return 404, {'message': 'unsupported port-control endpoint'}

    def _run(self):
        buffer = bytearray()
        try:
            while not self.stopping.is_set():
                part = self.stream.recv(65536)
                if not part: break
                buffer.extend(part)
                if len(buffer) > 131072: raise RuntimeError('port-control frame too large')
                while b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n'); buffer = bytearray(rest)
                    request = json.loads(line)
                    if not isinstance(request, dict) or type(request.get('id')) is not int:
                        raise ValueError('invalid request envelope')
                    try:
                        code, body = self.handle(request.get('method'), request.get('path'), request.get('body'))
                    except (ValueError, TypeError) as error:
                        code, body = 400, {'message': str(error)}
                    except (OSError, RuntimeError) as error:
                        code, body = 500, {'message': str(error)}
                    response = {'id': request['id'], 'status': code, 'body': body}
                    self.event(request=request, response=response)
                    self.stream.sendall(json.dumps(response, separators=(',', ':')).encode()+b'\n')
            if not self.stopping.is_set() and not self.shutdown_expected.is_set():
                self.error = 'guest port-control channel closed'
        except (OSError, ValueError, RuntimeError) as error:
            if not self.stopping.is_set(): self.error = str(error)
        finally:
            self.event(event='relay_exit', error=self.error, remaining_ports=list(self.ports.values()))

    def stop(self):
        self.stopping.set()
        try: self.stream.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.stream.close()
        self.thread.join(timeout=5)
        if self.thread.is_alive(): raise RuntimeError('port-control thread did not stop')
        self.log.close()
        return {'info_seen': self.info_seen, 'error': self.error, 'remaining_ports': list(self.ports.values())}

    def begin_shutdown(self):
        # Docker may still send DELETEs while shutting down. Keep serving until
        # the guest exits, but expect its serial channel to close afterwards.
        self.shutdown_expected.set()
