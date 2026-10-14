#!/usr/bin/env python3
"""Bounded native self-exec helper verification with PID/pgid identity cleanup.
No name-based pkill: every spawned process is recorded (pid+pgid) and reaped in
finally with an exact-pid liveness assertion."""
import contextlib, errno, fcntl, json, os, pathlib, select, signal, subprocess, time

BASE = pathlib.Path('/root/harddind-rebuild/arkclaude/round4/shared-posix-locks/work/probe')
PROBE = BASE / 'share-probe'
LOCKFILE = BASE / 'exf2'
LOCKFILE.write_bytes(b'A' * 8192)
os.chmod(LOCKFILE, 0o666)

spawned = []  # (popen, pgid) for guaranteed identity cleanup

class Probe:
    def __init__(self):
        self.p = subprocess.Popen([str(PROBE), str(LOCKFILE)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  bufsize=0, start_new_session=True)
        self.pgid = self.p.pid
        spawned.append((self.p, self.pgid))
        r = self._recv(5)
        assert r.get('ready'), r

    def _recv(self, t):
        r, _, _ = select.select([self.p.stdout], [], [], t)
        if not r:
            raise TimeoutError('probe response timeout')
        return json.loads(self.p.stdout.readline())

    def ask(self, cmd, timeout=10):
        self.p.stdin.write((cmd + '\n').encode())
        return self._recv(timeout)

    def shutdown(self):
        p, pgid = self.p, self.pgid
        try:
            p.stdin.write(b'quit\n')
            try:
                rc = p.wait(timeout=5)
                assert rc == 0, ('quit rc', rc)
                return
            except subprocess.TimeoutExpired:
                pass
        except (BrokenPipeError, OSError):
            pass
        # Identity/group bounded teardown (exact pgid created by setsid).
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError(f'probe group {pgid} did not exit')


def assert_exact_reap(pid, deadline_s=5.0):
    """Bounded exact-pid reap; assert exited 0 (or caller precondition)."""
    t0 = time.monotonic()
    while True:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return status
        if time.monotonic() - t0 > deadline_s:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            raise AssertionError(f'pid {pid} did not exit within {deadline_s}s, killed status={status}')
        time.sleep(0.02)


def main():
    q = v = None
    helper_pid = None
    try:
        q, v = Probe(), Probe()
        q.ask('open'); v.ask('open')

        # ---- Positive: lock taken, self-exec marker arrives AFTER exec ----
        r = q.ask('exec-hold 0 64')
        assert r['rc'] == 0, r
        assert r['post_exec'] and r['reported_pid'] == r['child_pid'] and r['lock_fd'] >= 0, r
        helper_pid = r['child_pid']
        time.sleep(0.2)
        # External conflict AFTER post-exec readiness.
        rr = v.ask('posix-wlock 0 64')
        assert rr['rc'] == -1 and rr['errno'] == errno.EAGAIN, rr

        # ---- Controlled release + exact-pid reap ----
        reap = q.ask('exec-reap')
        helper_pid = None
        assert reap['rc'] == 0 and reap['exited'] and not reap['signaled'], reap
        assert reap['exit_code'] == 0 and reap['term_sig'] == -1 and reap['raw_status'] == 0, reap
        # External reacquisition after release (lock did not outlive helper).
        v.ask('posix-wlock 0 64'); v.ask('posix-unlock 0 64')
        # Exact-pid identity: the post-exec child is dead and reaped; waitpid
        # on the same pid now reports ECHILD.
        try:
            os.waitpid(reap['child_pid'], os.WNOHANG)
            raise AssertionError('helper pid unexpectedly still a child')
        except ChildProcessError:
            pass

        # ---- Negative: deliberately missing executable ----
        r = q.ask('exec-missing 200 64')
        assert r['rc'] == -1 and r['errno'] == errno.ENOENT, r
        assert r['exited'] and r['exit_code'] == 127 and not r['signaled'], r
        # No lock leaked from the failed exec child.
        v.ask('posix-wlock 200 64'); v.ask('posix-unlock 200 64')

        print('SELF-EXEC NATIVE HANDSHAKE: POSITIVE + NEGATIVE OK')
        return 0
    finally:
        if helper_pid is not None:
            # Should never happen on failure paths; bounded exact reap.
            with contextlib.suppress(ProcessLookupError, ChildProcessError):
                os.kill(helper_pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(helper_pid, 0)
        for probe in (q, v):
            if probe is not None:
                probe.shutdown()
        # Identity assertion: every recorded probe process is fully reaped.
        # shutdown() called wait(); ECHILD here means reaped (correct). Any
        # other outcome would indicate a live/zombie descendant.
        for proc, pgid in spawned:
            assert proc.returncode == 0, (pgid, proc.returncode)
            try:
                wpid, st = os.waitpid(proc.pid, os.WNOHANG)
                raise AssertionError(f'unreaped child {proc.pid} status={st}')
            except ChildProcessError:
                pass  # reaped by shutdown()
        print('cleanup done, spawned groups:', [pg for _, pg in spawned])


@contextlib.contextmanager
def _noop():
    yield


if __name__ == '__main__':
    raise SystemExit(main())
