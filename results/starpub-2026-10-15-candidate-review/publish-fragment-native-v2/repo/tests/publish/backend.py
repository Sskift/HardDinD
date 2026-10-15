"""Spawn the real network-backend binary over a disposable AF_UNIX seqpacket
link with a private unix-dgram control socket. No devices, namespaces or
privileges are needed; libslirp binds ordinary host loopback sockets.
"""
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import time
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = pathlib.Path(os.environ.get('HARDDIND_NETWORK_BACKEND', REPO / '.work' / 'network-backend'))


class Backend:
    def __init__(self):
        if not BACKEND.exists():
            raise unittest.SkipTest(f'network-backend binary not found: {BACKEND}')
        self.tmp = None

    def start(self):
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='harddind-publish-'))
        self.ctl = self.tmp / 'network.ctl'
        self.link_host, link_guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.proc = subprocess.Popen(
            [str(BACKEND), str(link_guest.fileno()), str(self.ctl), '1', '1500'],
            pass_fds=(link_guest.fileno(),),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        link_guest.close()
        deadline = time.monotonic() + 5
        while not self.ctl.exists():
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode(errors='replace')
                raise RuntimeError('network-backend exited early:\n' + out)
            time.sleep(0.02)
        if time.monotonic() > deadline:
            raise RuntimeError('network-backend control socket never appeared')
        return self

    def command(self, text, timeout=3):
        path = self.tmp / ('c-' + os.urandom(4).hex())
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.bind(str(path))
            s.settimeout(timeout)
            try:
                s.connect(str(self.ctl))
                s.send(text.encode())
                reply = json.loads(s.recv(65536))
            finally:
                path.unlink(missing_ok=True)
        return reply

    def ok(self, text):
        reply = self.command(text)
        if reply.get('ok') is not True:
            raise AssertionError(f'{text!r} -> {reply}')
        return reply

    def fail(self, text):
        reply = self.command(text)
        if reply.get('ok') is not False:
            raise AssertionError(f'{text!r} unexpectedly succeeded: {reply}')
        return reply['error']

    def list(self):
        return self.command('list')

    def list_page(self, offset, limit):
        return self.command(f'list {offset} {limit}')

    def list_all_paged(self, mutate_between=None):
        """Aggregate a complete revision-consistent inventory. If
        mutate_between(page_just_read) is given it runs after each page; when
        it changes the table the sweep must restart (it returns True on
        restart)."""
        restarted = False
        for _ in range(16):
            rows, revision, offset = [], None, 0
            while True:
                page = self.list_page(offset, 64)
                if page.get('ok') is not True:
                    raise AssertionError(f'bad inventory page: {page}')
                if revision is None:
                    revision = page['revision']
                elif page['revision'] != revision:
                    restarted = True
                    break
                rows.extend(page['forwards'])
                total, limit = page['count'], page['limit']
                if mutate_between:
                    mutate_between()
                if offset + limit >= total:
                    assert len(rows) == total, (len(rows), total)
                    return rows, restarted
                offset += limit
            if not restarted:
                raise AssertionError('paged listing failed without revision change')
        raise AssertionError('inventory revision never stabilized')

    def stop(self):
        if getattr(self, 'stopped', False):
            return ''
        self.stopped = True
        result = None
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            result = self.proc.stdout.read().decode(errors='replace')
            self.link_host.close()
            if self.proc.returncode != 0:
                raise AssertionError(f'network-backend rc={self.proc.returncode}:\n{result}')
            if self.ctl.exists():
                raise AssertionError('backend left control socket behind')
        finally:
            import shutil
            if self.tmp:
                shutil.rmtree(self.tmp, ignore_errors=True)
        return result


@contextlib.contextmanager
def backend():
    b = Backend().start()
    try:
        yield b
    finally:
        b.stop()


def reserve_port(addr, udp=False, v6only=None):
    family = socket.AF_INET6 if ':' in addr else socket.AF_INET
    s = socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6 and v6only is not None:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        if not udp:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def can_bind(addr, port, udp=False, v6only=None, reuseaddr=True):
    family = socket.AF_INET6 if ':' in addr else socket.AF_INET
    s = socket.socket(family, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6 and v6only is not None:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        if reuseaddr:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, port))
        if not udp:
            s.listen(1)
        return True
    except OSError:
        return False
    finally:
        s.close()
