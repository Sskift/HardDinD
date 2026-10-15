"""Interactive shared-file probe, on the outer host or in a container."""
import json
import os
import select
import signal
import subprocess
import time


class Peer:
    def __init__(self, case, mode, path, uid=None, readonly=False):
        self.c, self.mode = case, mode
        self.buffer, self.closed, self.expected_kill = b'', False, False
        self.c.serial += 1
        self.name = self.c.object('container', 'peer-' + str(self.c.serial)) if mode == 'docker' else 'outer-peer'
        if mode == 'native':
            self.proc = self.c.spawn(command=[str(case.suite.bins / 'share_probe'), path.name,
                                             *([str(uid)] if uid is not None else [])], cwd=path.parent)
        else:
            self.proc = self.c.spawn('run', '--rm', '-i', '--name', self.name, '--label', self.c.label,
                                    *(['--user', f'{uid}:{uid}'] if uid is not None else []),
                                    '--mount', f'type=bind,src={path.parent},dst=/share' + (',readonly' if readonly else ''),
                                    'hd-functional:local', '/share_probe', '/share/' + path.name)
        try:
            row = self.receive()
            if row.get('ready') is not True:
                raise RuntimeError('probe not ready: ' + str(row))
        except BaseException:
            self.close()
            raise

    def receive(self, timeout=20):
        end = time.monotonic() + self.c.remaining(timeout)
        while b'\n' not in self.buffer:
            remain = end - time.monotonic()
            if remain <= 0 or not select.select([self.proc.stdout], [], [], remain)[0]:
                raise TimeoutError('probe response timeout')
            data = os.read(self.proc.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError('probe exited before response')
            self.buffer += data
        line, self.buffer = self.buffer.split(b'\n', 1)
        self.c.write(self.name + ': ' + line.decode(errors='replace'))
        return json.loads(line)

    def send(self, command):
        self.c.write(self.name + '> ' + command)
        self.proc.stdin.write((command + '\n').encode())
        self.proc.stdin.flush()

    def ask(self, command, ok=True):
        self.send(command)
        row = self.receive()
        if ok and row.get('rc') != 0:
            raise AssertionError(row)
        return row

    def pending(self):
        if self.buffer or select.select([self.proc.stdout], [], [], .15)[0]:
            raise AssertionError('wait returned before unlock/cancel')

    def interrupt(self):
        if self.mode == 'native':
            self.proc.send_signal(signal.SIGUSR1)
        else:
            self.c.d('kill', '--signal=USR1', self.name)

    def kill(self):
        self.expected_kill = True
        if self.mode == 'native':
            self.proc.kill()
        else:
            self.c.d('kill', '--signal=KILL', self.name)
        code = self.proc.wait(timeout=self.c.remaining(10))
        if code not in (-signal.SIGKILL, 137):
            raise AssertionError(f'kill returned {code}')

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.proc.poll() is None:
                self.send('quit')
                self.proc.stdin.close()
                # Container exit and --rm cleanup; the check's own deadline still bounds it.
                self.proc.wait(timeout=self.c.remaining(30))
            if not self.expected_kill and self.proc.returncode != 0:
                raise AssertionError(f'probe exited {self.proc.returncode}')
        finally:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=3)
            self.proc.stdin.close()
            self.proc.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_unused):
        self.close()
