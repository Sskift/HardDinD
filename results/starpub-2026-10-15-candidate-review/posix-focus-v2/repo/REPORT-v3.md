# 0005 shared POSIX locks — repair report v3 (stage 2, bounded)

Supersedes v1 (`evidence/v2/0005-rejected-v1.patch`) and stage-1 patch
`0886ea…` (frozen under the owner's `/root/harddind-rebuild/posix-owner-v2`,
Docker 38/38 per owner v2b). Patches 0001–0004 and the owner's main
(f38370c) were not touched; no guests/Docker/GPU/perf were run by this agent.
Owner snapshots `/root/harddind-rebuild/posix-owner-v{1,2}` and
`owner-hung-stage2/` were only read.

All builds: pinned rustc 1.98.1 (`48a229cea…`), ≤4 jobs, global build lock,
every cargo/test subprocess bounded with GNU `timeout --kill-after=30s`
(`scripts/build-virtiofs.sh`, `work/run-build.sh`). The 15-minute hang can no
longer hold the EX lock: the filtered suite now finishes in ~0.2 s.

## 1. Root cause of the hung test (owner evidence pid 1408534)

`PosixLockTable::wait_attempt` matched a registered waiter with

```rust
if let Some(waiter) = set.waiters.lock().unwrap().get(&unique) { … } else { /* open fd, fcntl */ }
```

The scrutinee's temporary `MutexGuard` lives for the **whole if expression**,
so on the first blocking attempt (else branch) the later
`set.waiters.lock().insert(…)` re-locked a non-reentrant `std::sync::Mutex`
on the same thread — a permanent self-deadlock; both threads parked in
`futex_wait_queue_me` exactly as captured. Fix: the lookup is now a scoped
block returning `Option<io::Result<Arc<OwnerEntry>>>` with the guard dropped
before any other lock is taken
(`src/passthrough/posix_locks.rs`, `wait_attempt`). Regression is covered by
`harddind_posix_wait_deadlock_flush_teardown_and_cancel` and
`harddind_posix_table_deadlock_flush_and_cancel`, both verified repeatedly
under the bounded test timeout.

## 2. Stage-2 semantics delivered

- **True SEEK_END / SEEK_CUR** (`src/share_probe.c`): `posix-wlock-end` now
  sends `l_whence=SEEK_END` with a signed `l_start` directly to fcntl (no
  lseek pre-resolution); `posix-wlock-cur` establishes/verifies a known file
  position and uses `l_whence=SEEK_CUR`. The guest kernel resolves these
  before FUSE; before-BOF is rejected `EINVAL` (proven natively:
  `-8193` on 8192 bytes → errno 22; `-8 8` → `[8184,8192)`).
- **Exact GETLK pid** (`posix_locks.rs` shadow interval set +
  `attribute()`): the guest tgid from `fuse_lk_in::lk.pid` is recorded per
  `(inode,owner)` on every SETLK and returned for guest-held OFD records;
  attribution matches by the same conflict rule the kernel uses (lowest
  overlapping record), not exact interval equality (the bug that caused the
  other v2 failures). Host-process holders report **pid 0**, so a host pid
  can never be confused with a coincidental guest pid. Split/merge, flush and
  lifetime are Rust-tested.
- **Deadlock detection**: a per-inode waiter graph returns `EDEADLK` for
  guest-owner cycles (A↔B verified in Rust and natively at the probe level,
  immediate, bounded). `FUSE_INTERRUPT`/flush removes the waiter; a flushed
  pending waiter returns `EINTR` and is proven not to acquire afterwards
  (real host mutation test, not just map absence).
- **exec preservation**: `exec-hold` proved that a POSIX lock taken in the
  child before `execve` survives (all inherited fds to the inode must clear
  CLOEXEC — closing one drops the process's locks after fork; this was found
  and fixed empirically), and is released when the exec'd program exits.
- **Rights**: a write lock on an O_RDONLY fd is `EBADF` (guest pre-wire and
  native); read locks share. The daemon opens owner fds honoring the guest
  handle's access mode with exact-mode fallback.
- **Cancel/grant races**: 8 bounded iterations of 5 ms SIGALRM vs unlock over
  the daemon's polling queue; both `EINTR` and grant accepted, leaks rejected.
- Stage-1 wire fixes retained: inclusive `end` (`(0,0)` = one byte;
  `start>0,end=0` invalid) and `OFFSET_MAX = 2^63-1`.

## 3. Bounded raw results

- Rust `harddind_` suite: **14/14 pass in 0.17–0.20 s**
  (`evidence/v3/rust-tests-bounded.log`), including 6 new lock-table/server
  tests; release build clean. Owner's exact `build-virtiofs.sh` RC=0
  (`evidence/v3/owner-build-bounded.log`).
- Native matrix `run-posix-native.py`: **26/26 pass**
  (`evidence/v3/posix-native-v9.json`): one-byte, adjacent exclusion, large
  offsets (`2^62`, final `2^63-1` byte, to-EOF GETLK), true SEEK_END/SEEK_CUR,
  before-BOF EINVAL, exact forked-child pid, two-process EDEADLK, exec
  preservation, O_RDONLY rights, cancel/grant races, rename/unlink, kill.
- Daemon smoke with the full HardDinD cmdline (chroot + default seccomp)
  creates the socket and serves.
- Full 0001–0005 series applies on the pristine pinned tree with `--fuzz=0`;
  applied tree is byte-identical to the tested tree.
- Hashes (`evidence/v3/stage2-hashes.txt`):
  - 0005 patch `b951faf536f48461ce958c696fde67b0c23855359e0cfe0e815948d1e11f48ed`
  - virtiofsd binary `b6b3f1bac0aa1c6e8bb7bea0f2f99cb4e51e02cc903f4151e3842cab18c87b3d`
  - series concatenation `8e220347becb54718ddec24e9c1482890ef0c4aaaed2138ab592e4b559c79899`
  - share_probe.c `9d80b55b…`, probe `a73a5cf7…`, run-shared-locks.py
    `939231dd…`, run-posix-native.py `7501e960…`
- Harness parity with owner fixes: `functional_complete` is written **before**
  JSON save; `posix_large_offsets` is in the executed list; `--functional-only`
  behavior matches the owner driver.

## 4. Docker matrix for the owner (I did not run it)

`run-shared-locks.py --posix-locks` now executes 27 posix cases per mode plus
the 19 flock/legacy cases: new `posix_whence_before_bof`,
`posix_getlk_child_exact_pid` (exact pid same pid-namespace),
`posix_getlk_host_pid_zero`, `posix_deadlock_cycle` (EDEADLK ≤10 s),
`posix_exec_preserves`, `posix_readonly_rights`, `posix_cancel_grant_race`.

```sh
bash scripts/build-share-probe.sh <work>
make virtiofs RUST_TOOLCHAIN=/path/to/rust-1.98.1
python3 scripts/run-shared-locks.py <work> <kernel> <disk> --name shared-posix-v3 \
    --virtiofsd <work>/virtiofsd --posix-locks --inline-flock --functional-only
```

## 5. Explicit remaining gaps (reported, not faked)

1. **Cross-pid-namespace pid=0**: FUSE carries no pid-namespace identity, so a
   lock held by a process in a *different* guest container namespace cannot
   be mapped to 0 vs a numeric value with certainty. Same-namespace children
   report exact tgids; host holders report 0; cross-container numbers are
   passed as the holder's own-namespace tgid and must be treated as opaque
   there. A kernel protocol extension would be needed for strict zero.
2. **EDEADLK scope**: the waiter graph detects cycles among *guest* owners.
   Deadlocks where the other edge is a host-process blocking lock cannot be
   detected from userspace polling (`F_OFD_SETLK` gives no deadlock info);
   such waits resolve when the host unlocks. Full POSIX cross-layer deadlock
   detection therefore remains incomplete and is marked as such rather than
   claimed.
3. **Permission-change reopen**: the exact-mode fallback is implemented and
   unit-covered for normal modes; the post-`chmod` reopening path against a
   running guest is guest-only validation and stays not_tested by me.
