"""Repeatable HTTP/TLS transfers without an external server or DNS."""
import contextlib
import hashlib
import http.server
import json
import ssl
import subprocess
import threading
import time
import urllib.request

SIZE = 8 * 1024 * 1024 + 17
REPEATS = 16
CHUNKS = (1460, 16384, 65536, 1048576)


def payload(size=SIZE):
    block = hashlib.shake_256(b'harddind-local-egress-v1').digest(65536)
    return (block * ((size + len(block) - 1) // len(block)))[:size]


def request_chunk(path):
    parts = path.strip('/').split('/')
    if len(parts) != 2 or parts[0] not in ('native', 'guest') or not parts[1].isdigit():
        raise ValueError('expected /native/N or /guest/N')
    return CHUNKS[int(parts[1]) % len(CHUNKS)]


@contextlib.contextmanager
def server(body, events, tls=None, timeout=10):
    class Server(http.server.ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(timeout)
            try:
                if tls:
                    connection = tls.wrap_socket(connection, server_side=True)
                return connection, address
            except Exception:
                connection.close()
                raise

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def do_GET(self):
            row = {'path': self.path, 'tls': tls is not None, 'started': time.time(), 'sent': 0}
            events.append(row)
            try:
                chunk = request_chunk(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                for offset in range(0, len(body), chunk):
                    part = body[offset:offset + chunk]
                    self.wfile.write(part)
                    row['sent'] += len(part)
                self.wfile.flush()
            except Exception as error:
                row['error'] = repr(error)
            finally:
                row['finished'] = time.time()
                self.close_connection = True

    host = Server(('127.0.0.1', 0), Handler)
    host.daemon_threads = True
    thread = threading.Thread(target=host.serve_forever, daemon=True)
    thread.start()
    try:
        yield host.server_port
    finally:
        host.shutdown()
        host.server_close()
        thread.join(timeout=5)


def run(c):
    body = payload()
    digest = hashlib.sha256(body).hexdigest()
    cert, key = c.root / 'cert.pem', c.root / 'key.pem'
    result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                             '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost'],
                            capture_output=True, text=True, timeout=c.remaining(15))
    if result.returncode:
        raise RuntimeError('local TLS certificate: ' + result.stderr)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    events, clients = [], []
    before = c.ctx.forward('stats')
    try:
        with contextlib.ExitStack() as stack:
            ports = [(scheme, stack.enter_context(server(body, events, context)))
                     for scheme, context in [('http', None), ('https', tls)]]
            for scheme, port in ports:
                for repeat in range(REPEATS):
                    started = time.monotonic()
                    url = f'{scheme}://127.0.0.1:{port}/native/{repeat}'
                    with urllib.request.urlopen(url, context=ssl._create_unverified_context(),
                                                timeout=c.remaining(10)) as response:
                        data = response.read()
                    if len(data) != len(body) or hashlib.sha256(data).hexdigest() != digest:
                        raise AssertionError('native local transfer corrupt: ' + url)
                    clients.append({'url': url, 'seconds': time.monotonic() - started, 'bytes': len(data)})
            name = c.object('container', 'downloads')
            urls = [f'{scheme}://10.0.2.2:{port}/guest' for scheme, port in ports]
            script = ('for base in "$@"; do i=0; while [ "$i" -lt ' + str(REPEATS) + ' ]; do '
                      'echo "BEGIN $base/$i"; wget --no-check-certificate -T 10 -qO /tmp/download "$base/$i"; '
                      f'test "$(wc -c </tmp/download)" -eq {len(body)}; '
                      f'echo "{digest}  /tmp/download" | sha256sum -c -; '
                      'echo "END $base/$i"; i=$((i+1)); done; done')
            c.d('run', '--rm', '--name', name, '--label', c.label, 'hd-base:latest',
                'sh', '-ec', script, 'sh', *urls, timeout=150)
            expected = REPEATS * len(ports) * 2
            if len(events) != expected or any(row.get('error') or row['sent'] != len(body) for row in events):
                raise AssertionError('local server did not complete every transfer')
            c.write(f'{expected} local transfers verified, {len(body)} bytes each; native + guest, HTTP + TLS')
    finally:
        data = {'size': len(body), 'sha256': digest, 'repeats': REPEATS, 'servers': events,
                'native_clients': clients, 'backend_before': before, 'backend_after': c.ctx.forward('stats')}
        (c.root / 'transfers.json').write_text(json.dumps(data, indent=2) + '\n')
        key.unlink(missing_ok=True)
