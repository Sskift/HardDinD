# 0005 shared POSIX locks — repair report v6 (self-exec lock-preservation helper)

Supersedes v5; owner's frozen `/root/harddind-rebuild/posix-owner-v5` and its
source/patch/binary were not modified. This is **test infrastructure only**:
the backend Rust patch `0005` (`a7ffeca5…`) and binary
`13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`) are
byte-identical/unchanged. No guest/GPU/performance runtime used. All host
compiles/tests ran under the global `kernel-build.lock` EX, ≤4 jobs, hard GNU
`timeout --kill-after`, bounded exact-PID reaping. All cleanup is by recorded
PID/process group — **no name-based pkill/pgrep** is used anywhere in the
final flow (the owner's process-cleanup note is complied with; identity-based
`/proc/<pid>/exe` scans only).

## 1. Defect (owner v5 Docker 45/46)

`posix_exec_preserves` failed: `exec-reap rc=-1 errno0`. Root causes:

1. The guest image at `run-shared-locks.py` contains **only `/share-probe`**;
   the probe's `execl("/bin/sleep", …)` fails with ENOENT in Docker (works on
   native, hiding the bug). The pre-exec ack was written **before** execve, so
   even a successful-looking run never proved post-exec lock retention.
2. The parent then consumed the failure path incorrectly and `exec-reap`
   found no child.

## 2. Fix: self-exec static helper (`src/share_probe.c`)

The probe re-execs **itself** in a dedicated mode (no external executable,
works in the minimal image):

```
share-probe --posix-exec-helper <ready_fd> <release_fd> <lock_fd> <timeout_ms>
```

Protocol, with exact ordering and failure accounting:

- Child takes the POSIX write lock on the **actual** open `fd` (lock start/len
  are command arguments), clears `CLOEXEC` **only** on the lock fd and the two
  pipe ends, then `execv("/proc/self/exe", helper args)` (argv[0]-absolute
  fallback).
- The new program image verifies the same lock fd is still open and emits a
  JSON readiness marker **after successful exec** containing its pid and the
  lock fd; the parent asserts `reported_pid == child_pid` and `lock_fd >= 0`
  **before** observing any external conflict — so retention across execve in
  the same pid with the same lock fd is actually proven.
- The helper then waits on `release_fd` (poll) or a **15 s hard timeout** —
  no sleep-based race. Parent writes a byte for controlled release.
- `exec-reap` reports `rc/exited/signaled/exit_code/term_sig/raw_status`
  after an exact-PID bounded `waitpid(WNOHANG)` loop (up to 5 s) then SIGKILL
  fallback. A missing `continue` that produced a duplicate pre-reap reply (the
  v5 `rc=-1 errno0`) is fixed on both success and reap paths.
- `exec-missing <start> <len>` deliberately execs an absent target and
  reports `errno` (ENOENT) + exit 127/raw status; all failure paths close the
  right pipe ends, reap the exact child, and leave no lock behind. Quit-time
  cleanup releases/reaps a lingering helper.

## 3. Bounded raw results

- **Real helper handshake regression**
  (`evidence/v6/exec-handshake-regression.py`, spawned with
  `start_new_session=True`, recorded pid+pgid, `finally` identity/group
  cleanup, exact-PID reap assertions): post-exec marker with same pid/lock fd,
  second-process EAGAIN conflict observed only after readiness, controlled
  release ⇒ `exit 0 / raw_status 0 / not signaled`, external reacquire;
  missing-executable ⇒ ENOENT / exit 127 with no surviving lock. **5/5
  bounded runs pass** (`evidence/v6/handshake-5x.txt`); no live/zombie
  descendants (recorded groups reaped; `/proc/<pid>/exe` identity scan empty).
- **Native matrix: 28/28 pass** (`evidence/v6/posix-native-v15.json`), new
  `posix_exec_preserves` (post-exec handshake) and `posix_exec_missing`
  (ENOENT/127 negative) included; run under EX with `timeout --kill-after=20`.
- Probe compiled under EX (`evidence/v6/build-probe-bounded.sh`),
  `-O2 -Wall -Wextra -Werror -static`, hard timeout; Python unit tests OK.
- Backend binary and 0005 patch untouched and re-hashed identical.

## 4. Docker harness for the owner (not run by me)

`run-shared-locks.py` `posix_exec_preserves` now drives the self-exec
handshake (post-exec marker, second guest container/process denied until
reap, exact status asserts) and new `posix_exec_missing` asserts ENOENT/127;
the imported image is the rebuilt static `/share-probe`, so helper mode is
present in the minimal image. Peer teardown uses process groups with bounded
exact-PID wait and a clean-shutdown assertion.

```sh
bash scripts/build-share-probe.sh <work>
python3 scripts/run-shared-locks.py <work> <kernel> <disk> --name shared-posix-v6 \
    --virtiofsd /root/harddind-rebuild/posix-owner-v5/run/virtiofsd \
    --posix-locks --inline-flock --functional-only
```

(The owner-v5 backend binary `13ebaff…` is unchanged; only the probe/harness
must be rebuilt/reimported.)

## 5. Hashes (`evidence/v6/stage6-hashes.txt`)

- share_probe.c `d56274c0ad8e8c208df2b673b7af93748da0b72bf89c150ea5bde7e2d6317266`
- share-probe binary `38611f38966f9cdb38d0e8123789166dfb058c8af0077e6b764f05cc0bbc589a`
- run-posix-native.py `fe9a5a5f75b70b91a92a50cf9d9c06a275fb63497275510634fd123c34aea8e0`
- run-shared-locks.py `dc843526721a1391eb5995f28e7f65b0ff94b1922107a02b02e74d7a0c04a952`
- virtiofsd (unchanged) `13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`
- 0005 patch (unchanged) `a7ffeca5fd5b847879d92b0f160035c88eee5440d6c95dd9b62b8c44ad51c37c`

## 6. Remaining limitations (unchanged from v5, backend)

Mixed host/guest blocking deadlock cycles and cross-pid-namespace GETLK
attribution remain pending and documented; this round changed only probe and
test harness code.
