#!/usr/bin/env python3
"""Deterministic native POSIX byte-range lock semantics matrix.

Drives two or more local share-probe processes (real host kernel locks) and a
host-process "outer" locker. This is the native half of the comparison;
run-shared-locks.py replays the same cases with peers inside Docker over the
virtiofs export. No guest, Docker or VM is involved here.
"""
import argparse
import contextlib
import errno
import fcntl
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import time

NAMES = [
    'posix_one_byte_zero',
    'posix_outer_excludes_inner', 'posix_inner_excludes_outer',
    'posix_non_overlapping', 'posix_readers_coexist',
    'posix_independent_processes', 'posix_self_multi_open',
    'posix_close_dup_releases', 'posix_fork_close_keeps_parent_lock',
    'posix_partial_unlock_split', 'posix_downgrade', 'posix_upgrade',
    'posix_to_eof', 'posix_blocking_grant', 'posix_signal_eintr',
    'posix_kill_releases', 'posix_whence_cur_end', 'posix_whence_before_bof',
    'posix_getlk_reports', 'posix_getlk_child_exact_pid', 'posix_deadlock_cycle',
    'posix_two_file_deadlock',
    'posix_exec_preserves', 'posix_readonly_rights', 'posix_cancel_grant_race',
    'posix_large_offsets', 'posix_rename_unlink',
]


class Peer:
    def __init__(self, probe, path, name='probe'):
        self.name = name
        self.buffer = b''
        self.expected_kill = False
        self.closed = False
        self.proc = subprocess.Popen([str(probe), path.name], cwd=path.parent,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0,
                                     start_new_session=True)
        row = self.receive()
        assert row.get('ready'), row
        self.pid = row['pid']

    def receive(self, timeout=10):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remain = deadline - time.monotonic()
            if remain <= 0 or not select.select([self.proc.stdout], [], [], remain)[0]:
                raise TimeoutError('probe response timeout')
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('probe exited before reply')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def send(self, command):
        self.proc.stdin.write((command + '\n').encode())

    def ask(self, command, ok=True):
        self.send(command)
        row = self.receive()
        if ok:
            assert row.get('rc') == 0, row
        return row

    def pending(self):
        assert not self.buffer and not select.select([self.proc.stdout], [], [], .2)[0], \
            'blocking lock returned before unlock/cancel'

    def interrupt(self):
        self.proc.send_signal(signal.SIGUSR1)

    def kill(self):
        self.expected_kill = True
        self.proc.kill()
        code = self.proc.wait(timeout=10)
        assert code in (-signal.SIGKILL, 137), code

    def close(self):
        if self.closed:
            return
        self.closed = True
        forced = False
        try:
            if self.proc.poll() is None:
                try:
                    self.send('quit')
                    self.proc.stdin.close()
                    self.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    forced = True
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=5)
        finally:
            self.proc.stdout.close()
            if not self.proc.stdin.closed:
                self.proc.stdin.close()
        assert not forced, 'forced peer shutdown'
        assert self.expected_kill or self.proc.returncode == 0, self.proc.returncode

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def outer_lock(file_, start, length, should_block, shared=False):
    operation = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    got = True
    try:
        fcntl.lockf(file_, operation, length, start, os.SEEK_SET)
    except BlockingIOError:
        got = False
    assert got != should_block, {'expected_blocked': should_block, 'acquired': got}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work', type=pathlib.Path)
    p.add_argument('--probe', type=pathlib.Path,
                   default=pathlib.Path(__file__).resolve().parents[1] / 'work' / 'probe' / 'share-probe')
    p.add_argument('--name', default='posix-native')
    a = p.parse_args()
    if not a.probe.is_file():
        p.error(f'probe not found: {a.probe} (build with build-share-probe.sh)')
    share = a.work / 'posix-native-share'
    share.mkdir(parents=True, exist_ok=True)
    checks = dict.fromkeys(NAMES, 'not_tested')
    errors = {}

    def case(name, fn):
        path = share / name
        path.write_bytes(b'A' * 8192)
        os.chmod(path, 0o666)
        try:
            with contextlib.ExitStack() as stack:
                fn(stack, path)
            checks[name] = 'pass'
        except BaseException as e:
            checks[name] = 'fail'
            errors[name] = f'{type(e).__name__}: {e}'
        print('native ' + name + ' ' + checks[name], flush=True)

    def peer(stack, path):
        q = Peer(a.probe, path)
        stack.enter_context(q)
        q.ask('open')
        return q

    def t_one_byte_zero(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 1')
        outer_lock(outer, 0, 1, True)
        outer_lock(outer, 1, 1, False)
        fcntl.lockf(outer, fcntl.LOCK_UN, 1, 1, os.SEEK_SET)
        q.ask('posix-unlock 0 1')
        outer_lock(outer, 0, 1, False)

    def t_outer_inner(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        fcntl.lockf(outer, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 0)
        row = q.ask('posix-wlock 0 64', ok=False)
        assert row['rc'] == -1 and row['errno'] in (errno.EAGAIN, errno.EACCES), row

    def t_inner_outer(stack, path):
        q = peer(stack, path)
        q.ask('posix-wlock 0 64')
        outer = stack.enter_context(path.open('r+b', buffering=0))
        outer_lock(outer, 0, 64, True)
        q.ask('posix-unlock 0 64')
        outer_lock(outer, 0, 64, False)

    def t_non_overlapping(stack, path):
        q = peer(stack, path)
        q.ask('posix-wlock 0 64')
        outer = stack.enter_context(path.open('r+b', buffering=0))
        outer_lock(outer, 64, 64, False)
        outer_lock(outer, 32, 64, True)
        outer_lock(outer, 64, 64, False, shared=True)

    def t_readers(stack, path):
        q = peer(stack, path)
        other = peer(stack, path)
        q.ask('posix-rlock 0 64')
        other.ask('posix-rlock 0 64')
        outer = stack.enter_context(path.open('r+b', buffering=0))
        outer_lock(outer, 0, 64, False, shared=True)
        outer_lock(outer, 0, 64, True)

    def t_independent(stack, path):
        q = peer(stack, path)
        other = peer(stack, path)
        q.ask('posix-wlock 0 64')
        row = other.ask('posix-wlock 0 64', ok=False)
        assert row['errno'] in (errno.EAGAIN, errno.EACCES), row
        row = other.ask('posix-rlock 0 64', ok=False)
        assert row['errno'] in (errno.EAGAIN, errno.EACCES), row
        other.ask('posix-wlock 64 64')

    def t_self_multi_open(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 64')
        q.ask('open-second')
        # Same owner through a second open description: no self-conflict.
        q.ask('posix-wlock2 0 64')
        outer_lock(outer, 0, 64, True)
        # Any close by the process releases its POSIX locks, even the other fd.
        q.ask('close-second')
        outer_lock(outer, 0, 64, False)
        fcntl.lockf(outer, fcntl.LOCK_UN, 64, 0, os.SEEK_SET)
        q.ask('posix-wlock 0 64')
        outer_lock(outer, 0, 64, True)
        q.ask('close')
        outer_lock(outer, 0, 64, False)

    def t_close_dup(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 64')
        q.ask('dup')
        q.ask('close')  # closes the original; dup description survives
        outer_lock(outer, 0, 64, False)
        fcntl.lockf(outer, fcntl.LOCK_UN, 64, 0, os.SEEK_SET)
        q.ask('posix-wlock2 0 64')
        outer_lock(outer, 0, 64, True)
        q.ask('close-second')
        outer_lock(outer, 0, 64, False)

    def t_fork_close(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 64')
        q.ask('fork-close')
        outer_lock(outer, 0, 64, True)

    def t_partial(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 128')
        q.ask('posix-unlock 64 64')
        outer_lock(outer, 64, 64, False)
        outer_lock(outer, 0, 64, True)
        outer_lock(outer, 128, 64, False)

    def t_downgrade(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 128')
        q.ask('posix-rlock 0 128')
        outer_lock(outer, 0, 128, False, shared=True)
        outer_lock(outer, 0, 128, True)

    def t_upgrade(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-rlock 0 64')
        q.ask('posix-wlock 0 64')  # sole owner may upgrade
        outer_lock(outer, 0, 64, True)
        q.ask('posix-unlock 0 64')
        outer_lock(outer, 0, 64, False)

    def t_to_eof(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 4000 0')
        outer_lock(outer, 100_000, 1, True)
        outer_lock(outer, 3999, 2, True)
        q.ask('posix-unlock 4000 0')
        outer_lock(outer, 100_000, 1, False)

    def t_blocking(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        fcntl.lockf(outer, fcntl.LOCK_EX, 0, 0)
        q.send('posix-wlock-w 10 64')
        q.pending()
        fcntl.lockf(outer, fcntl.LOCK_UN, 0, 0)
        row = q.receive()
        assert row['rc'] == 0, row
        q.ask('posix-unlock 10 64')

    def t_signal(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('signal-ready')
        fcntl.lockf(outer, fcntl.LOCK_EX, 0, 0)
        q.send('posix-wlock-w 0 64')
        q.pending()
        q.interrupt()
        row = q.receive()
        assert row['rc'] == -1 and row['errno'] == errno.EINTR, row
        fcntl.lockf(outer, fcntl.LOCK_UN, 0, 0)
        q.ask('posix-wlock 0 64')
        q.ask('posix-unlock 0 64')

    def t_kill(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 100 64')
        outer_lock(outer, 100, 64, True)
        q.kill()
        outer_lock(outer, 100, 64, False)

    def t_whence(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        # SEEK_END -8, length 8 -> [8184, 8192)
        q.ask('posix-wlock-end -8 8')
        outer_lock(outer, 8184, 8, True)
        outer_lock(outer, 8176, 8, False)
        # Offset set to 200, SEEK_CUR -> [200, 216)
        q.ask('posix-wlock-cur 200 16')
        outer_lock(outer, 200, 16, True)
        outer_lock(outer, 216, 16, False)

    def t_whence_before_bof(stack, path):
        q = peer(stack, path)
        row = q.ask('posix-wlock-end -8193 1', ok=False)
        assert row['rc'] == -1 and row['errno'] == errno.EINVAL, row

    def t_getlk_child_exact_pid(stack, path):
        # Forked child is a new files_struct in the same pid namespace; the
        # kernel GETLK reports the exact child tgid.
        q = peer(stack, path)
        row = q.ask('fork-locker 100 64')
        child = row['child_pid']
        reported = q.ask('posix-getlk 1 0 4096')
        assert reported['type'] == fcntl.F_WRLCK and reported['start'] == 100 \
            and reported['len'] == 64, reported
        assert reported['pid'] == child, (reported, child)
        q.ask('release-child')

    def t_deadlock_cycle(stack, path):
        # Bounded two-process cycle: kernel local detection returns EDEADLK.
        q = peer(stack, path)
        other = peer(stack, path)
        q.ask('posix-wlock 0 64')
        other.ask('posix-wlock 64 64')
        q.send('posix-wlock-w 64 64')
        q.pending()
        other.send('posix-wlock-w 0 64')
        row = other.receive(timeout=10)
        assert row['rc'] == -1 and row['errno'] == errno.EDEADLK, row
        other.ask('posix-unlock 64 64')
        assert q.receive(timeout=10)['rc'] == 0
        q.ask('posix-unlock 64 64')
        q.ask('posix-unlock 0 64')

    def t_two_file_deadlock(stack, path):
        # Real ABBA across two files, two processes.  The kernel must return
        # EDEADLK immediately; a plain one-way wait grants on unlock.
        xpath = path.with_name(path.name + '-x')
        ypath = path.with_name(path.name + '-y')
        xpath.write_bytes(b'A' * 8192); ypath.write_bytes(b'A' * 8192)
        os.chmod(xpath, 0o666); os.chmod(ypath, 0o666)
        qa = Peer(a.probe, xpath); stack.enter_context(qa)
        qb = Peer(a.probe, ypath); stack.enter_context(qb)
        qa.ask('open'); qb.ask('open')
        qa.ask('other-open ' + ypath.name)
        qb.ask('other-open ' + xpath.name)
        qa.ask('posix-wlock 0 64')
        qb.ask('posix-wlock 0 64')
        qa.send('posix-wlock-wy 0 64'); qa.pending()
        qb.send('posix-wlock-wy 0 64')
        row = qb.receive(timeout=10)
        assert row['rc'] == -1 and row['errno'] == errno.EDEADLK, row
        qb.ask('posix-unlock 0 64')
        assert qa.receive(timeout=10)['rc'] == 0
        qa.ask('posix-unlocky 0 64'); qa.ask('posix-unlock 0 64')
        # Non-cycle one-way wait: B waits X held by A; A unlocks -> grant.
        qa.ask('posix-wlock 128 64')
        qb.send('posix-wlock-wy 128 64'); qb.pending()
        qa.ask('posix-unlock 128 64')
        assert qb.receive(timeout=10)['rc'] == 0
        qb.ask('posix-unlocky 128 64')

    def t_exec_preserves(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        child = q.ask('exec-hold 3')['child_pid']
        assert child > 0
        outer_lock(outer, 0, 64, True)
        q.ask('exec-reap')
        fcntl.lockf(outer, fcntl.LOCK_UN, 64, 0, os.SEEK_SET)
        outer_lock(outer, 0, 64, False)

    def t_readonly_rights(stack, path):
        q = peer(stack, path)
        q.ask('close')
        q.ask('open-read')
        row = q.ask('posix-wlock 0 64', ok=False)
        assert row['rc'] == -1 and row['errno'] == errno.EBADF, row
        q.ask('posix-rlock 0 64')
        outer = stack.enter_context(path.open('r+b', buffering=0))
        outer_lock(outer, 0, 64, True)
        reader = stack.enter_context(path.open('rb', buffering=0))
        try:
            fcntl.lockf(reader, fcntl.LOCK_SH | fcntl.LOCK_NB, 64, 0)
            got = True
        except BlockingIOError:
            got = False
        assert got, 'read locks must coexist'
        q.ask('posix-unlock 0 64')

    def t_cancel_grant_race(stack, path):
        # Deterministic interrupt and grant outcomes.  Exclusion is verified by
        # a SECOND process (same-process fds share POSIX locks and never
        # conflict), never by an unlock that could mask a leaked acquisition.
        q = peer(stack, path)
        verifier = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        fcntl.lockf(outer, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 128)
        q.send('posix-timed-wait 128 64 5000')
        assert q.receive(timeout=5).get('armed') is True
        row = q.receive(timeout=10)
        assert row['rc'] == -1 and row['errno'] == errno.EINTR, row
        # Outer still holds; the EINTR waiter must NOT hold it: the separate
        # verifier process must be denied WITHOUT any unlock having been sent.
        assert verifier.ask('posix-wlock 128 64', ok=False)['errno'] == errno.EAGAIN
        fcntl.lockf(outer, fcntl.LOCK_UN, 64, 128)
        # Peer still doesn't hold it; verifier acquires once outer releases.
        verifier.ask('posix-wlock 128 64')
        verifier.ask('posix-unlock 128 64')
        fcntl.lockf(outer, fcntl.LOCK_EX | fcntl.LOCK_NB, 64, 256)
        q.send('posix-timed-wait 256 64 5000000')
        assert q.receive(timeout=5).get('armed') is True
        fcntl.lockf(outer, fcntl.LOCK_UN, 64, 256)
        row = q.receive(timeout=10)
        assert row['op'] == 'posix-timed-wait' and row['rc'] == 0, row
        # Granted peer excludes the separate verifier until explicit unlock.
        assert verifier.ask('posix-wlock 256 64', ok=False)['errno'] == errno.EAGAIN
        q.ask('posix-unlock 256 64')
        verifier.ask('posix-wlock 256 64')
        verifier.ask('posix-unlock 256 64')

    def t_getlk(stack, path):
        q = peer(stack, path)
        other = peer(stack, path)
        q.ask('posix-wlock 100 64')
        row = other.ask('posix-getlk 1 0 4096')
        assert row['type'] == fcntl.F_WRLCK and row['start'] == 100 and row['len'] == 64, row
        assert row['pid'] == q.pid, (row, q.pid)
        row = other.ask('posix-getlk 0 0 4096')
        assert row['type'] == fcntl.F_WRLCK, row
        row = other.ask('posix-getlk 1 1000 64')
        assert row['type'] == fcntl.F_UNLCK, row

    def t_large_offsets(stack, path):
        q = peer(stack, path)
        other = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        hi = 1 << 62
        last = (1 << 63) - 1
        q.ask(f'posix-wlock {hi} 8')
        outer_lock(outer, hi, 8, True)
        outer_lock(outer, hi - 1, 1, False)
        fcntl.lockf(outer, fcntl.LOCK_UN, 1, hi - 1, os.SEEK_SET)
        q.ask(f'posix-unlock {hi} 8')
        q.ask(f'posix-wlock {last} 1')
        outer_lock(outer, last, 1, True)
        outer_lock(outer, last - 1, 1, False)
        fcntl.lockf(outer, fcntl.LOCK_UN, 1, last - 1, os.SEEK_SET)
        q.ask(f'posix-unlock {last} 1')
        q.ask(f'posix-wlock {hi} 0')
        outer_lock(outer, last, 1, True)
        outer_lock(outer, hi - 1, 1, False)
        fcntl.lockf(outer, fcntl.LOCK_UN, 1, hi - 1, os.SEEK_SET)
        row = other.ask('posix-getlk 1 0 0')
        assert row['type'] == fcntl.F_WRLCK and row['start'] == hi and row['len'] == 0, row
        q.ask(f'posix-unlock {hi} 0')
        outer_lock(outer, last, 1, False)

    def t_rename_unlink(stack, path):
        q = peer(stack, path)
        outer = stack.enter_context(path.open('r+b', buffering=0))
        q.ask('posix-wlock 0 64')
        moved = path.with_suffix('.moved')
        path.rename(moved)
        outer_lock(outer, 0, 64, True)  # pre-rename fd references the same inode
        moved.unlink()
        outer_lock(outer, 0, 64, True)
        q.ask('close')
        outer_lock(outer, 0, 64, False)
        # A freshly created file at the old name is a different inode.
        path.write_bytes(b'B' * 4096)
        os.chmod(path, 0o666)
        outer2 = stack.enter_context(path.open('r+b', buffering=0))
        outer_lock(outer2, 0, 64, False)

    cases = {
        'posix_one_byte_zero': t_one_byte_zero,
        'posix_outer_excludes_inner': t_outer_inner,
        'posix_inner_excludes_outer': t_inner_outer,
        'posix_non_overlapping': t_non_overlapping,
        'posix_readers_coexist': t_readers,
        'posix_independent_processes': t_independent,
        'posix_self_multi_open': t_self_multi_open,
        'posix_close_dup_releases': t_close_dup,
        'posix_fork_close_keeps_parent_lock': t_fork_close,
        'posix_partial_unlock_split': t_partial,
        'posix_downgrade': t_downgrade,
        'posix_upgrade': t_upgrade,
        'posix_to_eof': t_to_eof,
        'posix_blocking_grant': t_blocking,
        'posix_signal_eintr': t_signal,
        'posix_kill_releases': t_kill,
        'posix_whence_cur_end': t_whence,
        'posix_whence_before_bof': t_whence_before_bof,
        'posix_getlk_reports': t_getlk,
        'posix_getlk_child_exact_pid': t_getlk_child_exact_pid,
        'posix_deadlock_cycle': t_deadlock_cycle,
        'posix_two_file_deadlock': t_two_file_deadlock,
        'posix_exec_preserves': t_exec_preserves,
        'posix_readonly_rights': t_readonly_rights,
        'posix_cancel_grant_race': t_cancel_grant_race,
        'posix_large_offsets': t_large_offsets,
        'posix_rename_unlink': t_rename_unlink,
    }
    for name in NAMES:
        case(name, cases[name])

    result = {'kind': 'posix_native_matrix', 'checks': checks, 'errors': errors,
              'all_passed': all(v == 'pass' for v in checks.values()),
              'probe': str(a.probe),
              'probe_sha256': __import__('hashlib').sha256(a.probe.read_bytes()).hexdigest()}
    out = a.work / 'results'
    out.mkdir(exist_ok=True)
    target = out / f'{a.name}.json'
    target.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'result': str(target), 'all_passed': result['all_passed']}, indent=2))
    return 0 if result['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
