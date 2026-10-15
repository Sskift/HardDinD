#!/usr/bin/env python3
"""Focused POSIX-lock semantics runner: fd-mode/chmod revocation and GETLK
PID-namespace identity.

Separate from run-posix-native.py / run-shared-locks.py so the owner can run
ONLY these new cases against the unchanged backend.

Modes:
  native : probes are local subprocesses (same host/guest-init pid namespace).
  docker : each probe is a guest container via scripts/harddind.py (the same
           CLI transport as run-shared-locks.py: explicit work directory,
           imported image, bind mount).

Outcomes per case: 'tested' (assertion proven) or 'not_tested' (only for an
exact EPERM/EACCES pidns-creation denial with recorded errno). Setup errors,
malformed responses and other errno values FAIL; nothing is silently skipped.

Process hygiene: every probe starts its own process group
(start_new_session=True); command pid/pgid/container identity and every case
result are persisted incrementally. Cleanup uses the recorded Popen handle /
container name with finite deadlines. No name-based kill.
"""
import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import time

from guest_cleanup import cleanup_group
PROBE_IDENTITIES = []
JOURNAL = lambda: None
TESTED, NOT_TESTED, FAIL = 'tested', 'not_tested', 'fail'

# errno values that legitimately mean "namespace creation not permitted here"
PIDNS_DENIED_ERRNOS = {errno.EPERM, errno.EACCES}


class ProbeTimeout(Exception):
    pass


_name_counter = [0]


def _next_name(prefix):
    _name_counter[0] += 1
    return f'{prefix}-focus-{_name_counter[0]}'


class Probe:
    """One share-probe process/container; identity/group cleanup only.

    The constructor reaps a spawned process itself if readiness fails, so a
    failure between Popen() and __enter__ cannot leak it.
    """

    def __init__(self, probe, path, mode, image='', name_prefix='posix-focus',
                 uid=None, work=None, pid_container=None):
        self.mode, self.path, self.uid = mode, path, uid
        self.name = None
        self.buf = b''
        self.proc = None
        self.closed = False
        self.identity = {'mode': mode, 'uid': uid, 'container': None,
                         'pid': None, 'pgid': None, 'path': str(path)}
        cwd = path.parent
        if mode == 'native':
            # Basename only: after setuid the child must not traverse root-only
            # parents (/root is 0700); Popen supplies cwd.
            cmd = [str(probe), path.name] + ([str(uid)] if uid is not None else [])
        else:
            if work is None:
                raise ValueError('docker mode requires explicit work directory')
            self.name = _next_name(name_prefix)
            self.identity['container'] = self.name
            repo = pathlib.Path(__file__).resolve().parents[1]
            harddind = repo / 'scripts' / 'harddind.py'
            cmd = [sys.executable, str(harddind), str(work), name_prefix,
                   'docker', 'run', '--rm', '-i', '--name', self.name,
                   '--mount', f'type=bind,src={path.parent},dst=/share']
            if pid_container:
                cmd += ['--pid', f'container:{pid_container}']
            # INTERNAL guest container only: permit pidns creation for the
            # nested case. Never touches outer host permissions.
            if os.environ.get('POSIX_FOCUS_ALLOW_NS') == '1':
                cmd += ['--cap-add', 'SYS_ADMIN',
                        '--security-opt', 'seccomp=unconfined']
            cmd += [image, '/share-probe', '/share/' + path.name]
            if uid is not None:
                cmd += [str(uid)]
        self.identity['cmd'] = cmd
        PROBE_IDENTITIES.append(self.identity)
        JOURNAL()
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
            self.identity['pid'] = self.proc.pid
            self.identity['pgid'] = os.getpgid(self.proc.pid)
            JOURNAL()
            row = self.recv(8)
            if not row.get('ready'):
                raise RuntimeError(f'bad ready row: {row}')
        except BaseException:
            with contextlib.suppress(Exception):
                self.close()
            raise

    def recv(self, timeout=10):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buf:
            remain = deadline - time.monotonic()
            if remain <= 0 or not select.select([self.proc.stdout], [], [], remain)[0]:
                raise ProbeTimeout('probe response timeout')
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                err = b''
                with contextlib.suppress(Exception):
                    err = self.proc.stderr.read() or b''
                raise RuntimeError(f'probe exited before reply: {err[:300]!r}')
            self.buf += chunk
        line, self.buf = self.buf.split(b'\n', 1)
        return json.loads(line)

    def ask(self, cmd, timeout=10):
        self.proc.stdin.write((cmd + '\n').encode())
        return self.recv(timeout)

    def close(self):
        if self.closed or self.proc is None:
            self.closed = True
            return
        self.closed = True
        p = self.proc
        forced = False
        try:
            if p.poll() is None:
                try:
                    with contextlib.suppress(OSError, BrokenPipeError):
                        p.stdin.write(b'quit\n')
                    with contextlib.suppress(Exception):
                        p.stdin.close()
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired, BrokenPipeError):
                    forced = True
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    with contextlib.suppress(ProcessLookupError):
                        p.kill()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            with contextlib.suppress(OSError):
                p.stdout.close()
            with contextlib.suppress(Exception):
                if not p.stdin.closed:
                    p.stdin.close()
        # forced shutdown is a real failure the caller records; returncode
        # surfaced for diagnostics but not asserted here (the runner decides).
        self.identity['returncode'] = p.returncode
        self.identity['forced'] = forced
        self.identity['cleanup'] = cleanup_group(p.pid)
        if self.mode == 'docker' and (forced or p.returncode != 0):
            cmd = self.identity['cmd'][:5] + ['rm', '-f', self.name]
            c = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                out, err = c.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(c.pid, signal.SIGKILL); out, err = c.communicate(timeout=5)
            self.identity['container_cleanup'] = {'pid':c.pid, 'rc':c.returncode, 'stdout':out.decode(errors='replace'), 'stderr':err.decode(errors='replace'), 'cleanup':cleanup_group(c.pid)}
        with contextlib.suppress(OSError):
            p.stderr.close()
        JOURNAL()
        if forced or p.returncode != 0 or not self.identity['cleanup']['clean_shutdown']:
            raise RuntimeError('probe cleanup failed: ' + str(self.identity))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def run_cases(work, probe, mode, image, name_prefix, sink):
    share = work / 'posix-focus-share'
    share.mkdir(parents=True, exist_ok=True)

    def record(name, status, detail):
        sink(name, {'status': status, 'detail': detail,
                    'ts': round(time.monotonic(), 3)})

    # ---- FD access mode gates (kernel f_mode; root cannot bypass) ----
    p = share / 'fdmode.dat'
    p.write_bytes(b'A' * 8192)
    os.chmod(p, 0o666)
    with Probe(probe, p, mode, image, name_prefix, work=work) as q:
        q.ask('open-read')
        r = q.ask('posix-wlock 0 64')
        record('fdmode_rdonly_wrlock_ebadf',
               TESTED if r['rc'] == -1 and r['errno'] == errno.EBADF else FAIL, r)
        r = q.ask('posix-rlock 0 64')
        record('fdmode_rdonly_rdlock_ok', TESTED if r['rc'] == 0 else FAIL, r)
        q.ask('posix-unlock 0 64'); q.ask('close')
        q.ask('open-wronly')
        r = q.ask('posix-rlock 0 64')
        record('fdmode_wronly_rdlock_ebadf',
               TESTED if r['rc'] == -1 and r['errno'] == errno.EBADF else FAIL, r)
        r = q.ask('posix-wlock 0 64')
        record('fdmode_wronly_wrlock_ok', TESTED if r['rc'] == 0 else FAIL, r)
        q.ask('posix-unlock 0 64')

    # ---- Post-open chmod 000, UNPRIVILEGED owner uid ----
    p = share / 'chmod-revoke.dat'
    p.write_bytes(b'A' * 8192)
    uid = 42424
    os.chmod(p, 0o600)
    os.chown(p, uid, uid)
    with contextlib.ExitStack() as st:
        q = st.enter_context(Probe(probe, p, mode, image, name_prefix, uid=uid, work=work))
        other = st.enter_context(Probe(probe, p, mode, image, name_prefix, uid=uid, work=work))
        q.ask('open'); other.ask('open')
        os.chmod(p, 0o000)
        r = q.ask('posix-wlock 0 64')
        record('chmod000_held_fd_wrlock_ok', TESTED if r['rc'] == 0 else FAIL, r)
        r = q.ask('reopen rdwr')
        record('chmod000_unpriv_reopen_eacces',
               TESTED if r['rc'] == -1 and r['errno'] == errno.EACCES else FAIL, r)
        r = other.ask('reopen rdwr')
        record('chmod000_unpriv_reopen_eacces_second_process',
               TESTED if r['rc'] == -1 and r['errno'] == errno.EACCES else FAIL, r)
        r = other.ask('posix-wlock 0 64')
        record('chmod000_held_lock_conflicts',
               TESTED if r['rc'] == -1 and r['errno'] == errno.EAGAIN else FAIL, r)
        q.ask('posix-unlock 0 64')
        os.chmod(p, 0o666)
        os.chown(p, 0, 0)

    # ---- Root CAP_DAC_OVERRIDE informational (root reopen mode000 succeeds) ----
    p = share / 'chmod-root.dat'
    p.write_bytes(b'A' * 8192)
    os.chmod(p, 0o000)
    with Probe(probe, p, mode, image, name_prefix, work=work) as q:
        q.ask('open')
        r = q.ask('reopen rdwr')
        record('root_dac_override_reopen_mode000_informational',
               TESTED if r['rc'] == 0 else FAIL,
               {**r, 'note': 'expected root success; DAC tests must use uid'})
    os.chmod(p, 0o666)

    # ---- GETLK same pid namespace: holder AND querier in ONE probe process
    # (fork child holds, parent queries). Correct in BOTH modes; two separate
    # docker containers would be sibling pid namespaces, not the same one. ----
    p = share / 'getlk-samens.dat'
    p.write_bytes(b'A' * 8192)
    os.chmod(p, 0o666)
    with Probe(probe, p, mode, image, name_prefix, work=work) as q:
        q.ask('open')
        row = q.ask('fork-locker 100 64')
        child_pid = row.get('child_pid')
        rep = q.ask('posix-getlk 1 0 4096')
        ok = (rep.get('type') == fcntl.F_WRLCK and rep.get('start') == 100
              and rep.get('len') == 64 and rep.get('pid') == child_pid)
        record('getlk_same_pidns_exact_child_tgid',
               TESTED if ok else FAIL,
               {'reported': rep, 'child_pid': child_pid,
                'querier': 'same probe process (fork child)',
                'identity': q.identity})
        q.ask('release-child')

    # ---- GETLK across SIBLING pid namespaces (docker only): holder in one
    # container, querier in a SEPARATE container -> kernel reports pid 0. ----
    p = share / 'getlk-sibling.dat'
    p.write_bytes(b'A' * 8192)
    os.chmod(p, 0o666)
    if mode == 'native':
        record('getlk_sibling_container_pid_zero', NOT_TESTED,
               {'reason': 'sibling pid namespaces require separate guest '
                          'containers; run --mode docker',
                'native_control': 'getlk_sibling_native_same_ns_exact '
                                  '(in run-posix-native.py) proves exact tgid '
                                  'when holder and querier share the pidns'})
    else:
        with contextlib.ExitStack() as st:
            holder = st.enter_context(Probe(probe, p, mode, image, name_prefix, work=work))
            querier = st.enter_context(Probe(probe, p, mode, image, name_prefix, work=work))
            holder.ask('open'); querier.ask('open')
            row = holder.ask('fork-locker 200 64')
            child_pid = row.get('child_pid')
            rep = querier.ask('posix-getlk 1 0 4096')
            ok = rep.get('type') == fcntl.F_WRLCK and rep.get('pid') == 0
            record('getlk_sibling_container_pid_zero', TESTED if ok else FAIL,
                   {'reported': rep, 'holder_child_pid': child_pid,
                    'holder_container': holder.identity['container'],
                    'querier_container': querier.identity['container'],
                    'expect': 'locks_translate_pid -> 0 invisible sibling'})
            holder.ask('release-child')

    # ---- Nested pid namespace via unshare INSIDE a process/container ----
    p = share / 'getlk-nested.dat'
    p.write_bytes(b'A' * 8192)
    os.chmod(p, 0o666)
    with Probe(probe, p, mode, image, name_prefix, work=work) as q:
        q.ask('open')
        r = q.ask('ns-nested-lockcheck 0 64', timeout=15)
        if r.get('rc') == 0:
            expect = {'unshare_errno': 0, 'holder_local_pid': 1,
                      'getlk_local_pid': 1, 'querier_local_pid': 2}
            ok = all(r.get(k) == v for k, v in expect.items()) \
                and isinstance(r.get('holder_ancestor_pid'), int) \
                and r['holder_ancestor_pid'] > 0
            record('getlk_nested_pidns_local_vs_ancestor', TESTED if ok else FAIL, r)
        else:
            ue = r.get('unshare_errno')
            if ue in PIDNS_DENIED_ERRNOS:
                record('getlk_nested_pidns_local_vs_ancestor', NOT_TESTED,
                       {'unshare_errno': ue, 'raw': r,
                        'reason': 'pidns creation denied; run in the guest '
                                  'container with POSIX_FOCUS_ALLOW_NS=1'})
            else:
                # malformed response / lock failure / unexpected errno FAIL.
                record('getlk_nested_pidns_local_vs_ancestor', FAIL, r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('work', type=pathlib.Path)
    ap.add_argument('--probe', type=pathlib.Path)
    ap.add_argument('--mode', choices=('native', 'docker'), default='native')
    ap.add_argument('--image', default='harddind-lock-probe:local')
    ap.add_argument('--name-prefix', default='posix-focus')
    ap.add_argument('--out', type=pathlib.Path)
    a = ap.parse_args()
    probe = a.probe or (pathlib.Path(__file__).resolve().parents[1] / 'work' / 'probe' / 'share-probe')
    if not probe.is_file():
        ap.error(f'probe missing: {probe} (build scripts/build-share-probe.sh)')
    work = a.work.resolve()
    out = a.out or (work / 'results' / f'posix-focus-{a.mode}.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    results = {}

    def sink(name, payload):
        results[name] = payload
        # Incremental persistence: identity + every case result flushed now,
        # so a setup/teardown failure leaves an on-disk partial record.
        partial = _report(a.mode, probe, results, started, complete=False)
        tmp = out.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(partial, indent=2) + '\n')
        tmp.replace(out)
        print(f"{name}: {payload['status']} "
              f"{json.dumps(payload['detail'], sort_keys=True)}", flush=True)

    def journal():
        partial = _report(a.mode, probe, results, started, complete=False)
        tmp = out.with_suffix('.json.tmp'); tmp.write_text(json.dumps(partial, indent=2) + '\n'); tmp.replace(out)
    global JOURNAL
    JOURNAL = journal
    journal()
    rc = 0
    try:
        run_cases(work, probe.resolve(), a.mode, a.image, a.name_prefix, sink)
    except BaseException as e:
        results['_runner_error'] = {
            'status': FAIL,
            'detail': {'type': type(e).__name__, 'message': str(e),
                       'ts': round(time.monotonic(), 3)}}
        rc = 1

    failed = [k for k, v in results.items() if v['status'] == FAIL]
    report = _report(a.mode, probe, results, started, complete=True, rc=rc)
    tmp = out.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report, indent=2) + '\n')
    tmp.replace(out)
    print(json.dumps({'tested': report['tested'], 'not_tested': report['not_tested'],
                      'failures': failed}), flush=True)
    return 1 if failed else 0


def _report(mode, probe, results, started, complete, rc=0):
    tested = sum(1 for v in results.values() if v['status'] == TESTED)
    nt = sum(1 for v in results.values() if v['status'] == NOT_TESTED)
    return {
        'kind': 'posix-focus', 'peers': PROBE_IDENTITIES, 'mode': mode, 'complete': complete, 'rc': rc,
        'probe': str(probe),
        'probe_sha256': hashlib.sha256(probe.read_bytes()).hexdigest(),
        'allow_ns': os.environ.get('POSIX_FOCUS_ALLOW_NS', '0'),
        'tested': tested, 'not_tested': nt,
        'failures': [k for k, v in results.items() if v['status'] == FAIL],
        'runtime_s': round(time.time() - started, 3),
        'checks': results,
        'pending_guest': [
            'getlk_sibling_container_pid_zero (sibling guest containers -> 0)',
            'getlk_nested_pidns_local_vs_ancestor (NEWUSER|NEWPID inside guest)',
        ] if mode == 'native' else [],
        'baseline_limitation': [
            'nested pidns not_tested only for unshare EPERM/EACCES',
            'internal guest container needs SYS_ADMIN + seccomp=unconfined '
            '(POSIX_FOCUS_ALLOW_NS=1); outer host permissions are unchanged',
            'mixed host/guest blocking deadlock cycles undetectable from userspace',
        ],
    }


if __name__ == '__main__':
    sys.exit(main())
