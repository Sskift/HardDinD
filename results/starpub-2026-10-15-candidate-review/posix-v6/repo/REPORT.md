# 0005 shared POSIX byte-range locks — implementation report

Round: `shared-posix-locks`. Scope: real cross-layer POSIX record locks in pinned
virtiofsd 1.13.2, opt-in, on top of the preserved 0001–0004 series.

## 1. Result

`virtiofs/patches/0005-shared-posix-locks.patch` (sha256
`0b69f4349b0c0de0b714c50036b2b199fd879861f3786d77a5e02f712881061f`) implements
FUSE `GETLK`/`SETLK`/`SETLKW` with genuine per-guest-process ownership:

- New daemon flag `--shared-posix-locks` (`src/main.rs:159-167`), plumbed into
  `passthrough::Config` (`src/passthrough/mod.rs:346-349`, default `false`) and
  the HardDinD launcher (`scripts/harddind.py`: `--share-posix-locks` adds
  `--shared-posix-locks` to the daemon command).
- INIT negotiates `FUSE_POSIX_LOCKS` only when enabled **and** the client is
  capable; otherwise `EPROTO` so the guest can never silently fall back to
  local locks (`src/passthrough/mod.rs:1621-1630`; unit test at
  `src/passthrough/mod.rs:3236`).
- One host open file description per `(inode, FUSE lock_owner)` in a new table
  (`src/passthrough/posix_locks.rs:47-131`); all of an owner's operations are
  `F_OFD_GETLK`/`F_OFD_SETLK` on that fd. Blocking `F_SETLKW` rides the
  existing cancellable retry queue (0002): nonblocking attempts polled off the
  worker pool, `FUSE_INTERRUPT`/SIGKILL maps to `EINTR` without ever taking a
  lock on the cancelled path (`src/vhost_user.rs:263-283`,
  `src/server.rs:1298`).
- `FLUSH`, sent by the guest for every `close(2)`, closes the owner fd, which
  releases all of that owner's locks on the inode exactly per POSIX
  (`src/passthrough/mod.rs:2429-2441`, table call at
  `src/passthrough/posix_locks.rs:111-120`). Forget/DESTROY evict fds
  (`posix_locks.rs:123-131`, `mod.rs:1644`, `1672-1689`).
- Migration remains rejected (`src/passthrough/device_state/mod.rs:74,95`).
  No new seccomp rules, host privileges or services: only existing
  `fcntl`/`openat`/`close` syscalls are used (`src/seccomp.rs:100,151`).

### Why per-owner OFD fds are correct here

FUSE `lock_owner` identifies the guest `struct files_struct` (XTEA-scrambled
pointer, guest kernel `fs/fuse/file.c:427-443`, filled at
`fs/fuse/file.c:2491-2513`): threads/`dup(2)`/`execve(2)` share it; `fork(2)`
gets a new one. A daemon cannot represent multiple such owners with
traditional POSIX locks (one daemon PID) nor with per-open OFD locks (two opens
of one guest process would self-conflict and closing one fd would not release
locks taken on another). A dedicated fd per `(inode,owner)` with OFD locks
matches every rule:

| requirement | mechanism | evidence |
|---|---|---|
| independent guest processes conflict, read vs write, overlapping ranges | different fds ⇒ OFD conflicts | native cases, Rust test |
| same owner across multiple opens/dups never self-conflicts, splits/merges | all ops share one fd | `posix_self_multi_open`, `posix_partial_unlock`, `posix_downgrade_upgrade` |
| any close releases the owner's locks (even via another fd) | FLUSH closes the owner fd | `posix_close_dup_releases`, `posix_self_multi_open` |
| fork child closing inherited fd does not release parent locks | different owner id, no entry | `posix_fork_close` |
| host-native locks conflict both directions | OFD and POSIX locks share the kernel lock context | host OFD probe, `posix_outer_excludes_inner`/`posix_inner_excludes_outer` |
| blocking grant / signal / SIGKILL | retry queue + FLUSH | `posix_blocking_grant`, `posix_signal_eintr`, `posix_kill_release` |
| SEEK_CUR/SEEK_END and signed starts | guest VFS resolves before FUSE (`flock_to_posix_lock`); daemon validates absolute u64 ranges | `posix_whence_cur_end`, parser unit test |
| rename/unlink/inode identity | owner fds reopen through the inode's `/proc/self/fd` O_PATH fd | `posix_rename_unlink`, Rust unlink test |
| teardown races | per-inode serialization orders attempts vs FLUSH/forget; cancel path never locks | `posix_locks.rs:51-56,85-109`, queue tests from 0002 still pass |

Protocol details (pinned kernel `/root/harddind-rebuild/linux-7.2.10`): the
client sends absolute `start/end`, `end=OFFSET_MAX (2^62-1)` for to-EOF
(legacy `0` accepted); the daemon converts inclusive-end to `l_len`
(`src/server.rs:93-132`). GETLK replies `F_UNLCK` echoing the query range; a
holder reports its range with the host pid (`-1`/u32 `0xFFFFFFFF` for OFD
holders). The guest keeps **no** local POSIX lock state when `.lock` is present
(`fs/locks.c:2516-2519`) and does not send unlock-at-close; FLUSH is the
release mechanism (`fs/fuse/file.c:467-518`), also delivered during SIGKILL
teardown.

## 2. Test artifacts

- `src/share_probe.c` (sha256
  `ce22a7b44302481f4a896e217b09cae03d616cee29e81da8d27706d5a76ee566`):
  byte-range commands `posix-rlock`/`posix-wlock`/`posix-unlock` (nonblock),
  `*-w` (F_SETLKW), trailing `2` selects the second/dup fd, `posix-wlock-cur`,
  `posix-wlock-end`, `posix-getlk <type> <start> <len>`, plus `fork-close`.
  Builds `-Wall -Wextra -Werror -static`.
- `scripts/run-posix-native.py` (new): deterministic 18-case native matrix,
  no guest/Docker needed.
- `scripts/run-shared-locks.py` (extended): `--posix-locks` adds the same 15
  cross-layer cases to **both** native and Docker(guest) suites; report gains a
  `posix_locks` field. The two legacy cases remain unconditional, so the
  pre-existing failure evidence stays reproducible when the flag is off.
- `scripts/harddind.py` (extended): `--share-posix-locks` forwards
  `--shared-posix-locks` and is included in instance-config identity checks.

## 3. What was tested here (no guest/Docker used by this agent)

1. Host kernel semantics probe `work/probe/ofd.c`: OFD-vs-OFD conflicts,
   bidirectional OFD↔traditional POSIX conflict, same-fd self-visibility,
   `F_GETLK` pid = -1 for OFD and host pid for POSIX holders, to-EOF `l_len=0`,
   release on close — all as the design assumes (host kernel 5.15).
2. `cargo test --release --locked harddind_` with the **exact pinned
   toolchain rustc 1.98.1 (`48a229cea…`)**: 10/10 pass, including five new
   tests — range/owner isolation/bidirectional host conflict/GETLK pids/flush
   (`passthrough/mod.rs:3022`), multi-open/rename/unlink/retry grant
   (`mod.rs:3166`), hard-fail when negotiation is missing (`mod.rs:3236`),
   table flush/forget (`posix_locks.rs:174`), wire parser
   (`server.rs:1895`).
3. `cargo build --release --locked` succeeds; the owner's unmodified build
   flow `bash scripts/build-virtiofs.sh <work> <pinned-toolchain>` (now capped
   to 4 jobs per the environment rule) completes and emits binary sha256
   `6f02969fac669a63420585cc66d4e8daebee48f813a3409915131a0053f2fdd9`,
   patch-series sha256
   `55a8a6d914ceae0690fe2e85f25e191327927b4b2345e3e419aaa8a0137f763d`
   (`evidence/build-hashes.txt`).
4. Daemon smoke: `--shared-posix-locks` without a pool fails
   `LocksNeedPool`; the full HardDinD cmdline (chroot + default seccomp)
   creates the socket and serves; SIGTERM behavior matches the existing
   binary.
5. Native matrix `scripts/run-posix-native.py` — 18/18 pass
   (`evidence/posix-native-v3.json`, all_passed=true).
6. Full patch series applies on the pristine pinned tarball with
   `patch --fuzz=0`; resulting tree is byte-identical to the tested tree.
   Python unit tests (20) and script syntax checks pass. `make check`'s only
   failure is the pre-existing `guest/*` glob (this round4 workspace contains
   no `guest/` directory; unrelated to this change).

## 4. What remains for the owner (guest/Docker — explicitly NOT tested here)

Run the full guest acceptance (builds `work/virtiofsd` from this series):

```sh
# the probe gained byte-range commands; rebuild/reimport it first:
bash scripts/build-share-probe.sh <work>
make virtiofs RUST_TOOLCHAIN=/path/to/rust-1.98.1
# VM/Docker comparison, native + guest suites, immutable binary hashes:
python3 scripts/run-shared-locks.py <work> <kernel> <disk> --name shared-posix-v1 \
    --virtiofsd <work>/virtiofsd --posix-locks --inline-flock
# native-only re-run, no VM:
python3 scripts/run-posix-native.py <work>
```

Expected: all 19 flock/legacy cases plus all 15 `posix_*` cases pass in both
native and docker modes, including `posix_independent_guest_processes` (two
containers = two FUSE owners must conflict, previously collapsed), and the two
legacy cases that fail in `results/shared-flock-v2.json` etc. turn green.
`benchmark_complete` and clean teardown assertions are unchanged.

## 5. Known limitations (reported, not faked)

- `GETLK` pid has no guest pid-namespace translation: guest-held OFD locks
  report pid 0/-1, host-held locks report the host pid (same limitation as
  QEMU virtiofsd's OFD implementation).
- Blocking waits are 2 ms nonblocking polls on the shared 0002 queue (idle
  cost zero; bounded grant latency), not `F_OFD_SETLKW` threads — this is what
  preserves the proven interrupt/grant race semantics and worker-pool budget.
- Lock state is not migrated (`EOPNOTSUPP`, consistent with 0002).
- Owner fds open `O_RDWR`, falling back to `O_RDONLY` on `EACCES/EROFS`;
  write locks on the fallback return `EBADF` as on a read-only open.
- The mechanism is validated against the pinned guest kernel 7.2.10 FUSE
  client behavior; other clients were not exercised.

## 6. Boundaries preserved

- 0001–0004 patches untouched; 0005 only adds the eight files in the diffstat.
- Prior failure evidence under `/root/harddind-rebuild/results` not modified;
  with `--posix-locks` off behavior is unchanged (legacy cases still fail
  honestly instead of locking locally).
- No changes to `/root/udock`, host privileges, seccomp, the pinned sources in
  `/root/harddind-rebuild/virtiofsd-1.13.2`, or main remote
  scripts/kernel/virtiofs trees. All work stayed under this round directory;
  sustained builds held `/root/harddind-rebuild/kernel-build.lock` and used
  ≤4 jobs.
