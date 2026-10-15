# 0005 shared POSIX locks — repair report v4 (cross-inode guest deadlock)

Supersedes v3, frozen by the owner at
`/root/harddind-rebuild/posix-owner-review-v3` (patch
`b951faf536f48461ce958c696fde67b0c23855359e0cfe0e815948d1e11f48ed`) which was
not touched. Patches 0001–0004 and owner main were not touched; no
guest/GPU/performance runtime was used. Every build/test ran bounded under the
global EX lock (normal wait on the owner's diagnostic queue), ≤4 jobs, GNU
`timeout --kill-after=30s` in both `scripts/build-virtiofs.sh` and
`work/run-build.sh` (tests 180s, build 600s). The full filtered suite finishes
in ~0.2 s, so a repeat of the v3 15-minute lock hold is impossible.

## 1. Defect repaired

v3's wait-for graph lived inside `InodeLockSet`, so edges could only be formed
between waiters and granted owners **on one inode**. The routine all-guest
cycle — process A holds file X and waits for Y, process B holds Y and waits for
X — was invisible and would have polled to a timeout instead of returning
`EDEADLK`. This is distinct from the documented host/guest mixed-cycle
limitation and is now fixed.

## 2. Implementation (`src/passthrough/posix_locks.rs`)

- The waiter registry is now a single **global**
  `Mutex<BTreeMap<unique, Waiter>>` in `PosixLockTable`; each `Waiter` records
  its `inode`, owner entry, type and byte range. `InodeLockSet` keeps only its
  per-inode teardown lock, owners and granted intervals.
- Cycle detection is a DFS over the global guest-owner wait-for graph, where
  an edge `owner -> blocker` is recomputed live from the blocker's GRANTED
  intervals on the waiter's inode (`blockers_of`, byte-range aware; dead
  entries skipped; host-process holders deliberately not represented).
- **Racing false positives are prevented**: detection runs in two phases. A
  cheap snapshot may flag a candidate; before returning `EDEADLK`, every inode
  on the candidate cycle is locked in ascending `Inode` order (deadlock-free
  lock ordering, never re-entrant) together with the global waiter mutex, and
  reachability is recomputed against frozen grants. A cycle that collapsed
  (unlock/cancel between snapshot and verify) returns plain `EAGAIN` and is
  retried — no timeout-based or map-only PASS is ever manufactured.
- Lock-order discipline is documented at the registry: per-inode
  `serialize` → global `waiters` → owner `intervals` (leaf); `inodes` is only
  briefly mapped/cloned.
- Edge removal semantics preserved and sharpened:
  - `wait_cancel(inode, unique)` (FUSE_INTERRUPT/teardown) removes exactly
    that waiter; canceled edges cannot manufacture a cycle (tested).
  - `flush_owner` marks the per-file owner entry dead and removes it from the
    owners map; its waiters **on that inode** are excluded from the graph and
    fail `EINTR` on their next poll. POSIX locks are per (process, file):
    flushing file X does not cancel the same owner's pending wait on a
    different file Y (tested both directions); process death flushes each open
    file and forget/DESTROY purge the registry.
- Only the waiter that closes the cycle is unregistered on `EDEADLK`; other
  edges keep failing until holders unlock, matching Linux behavior.

## 3. Bounded raw results (no PASS manufactured from timeout)

Rust `harddind_` suite: **16/16 pass in 0.18–0.23 s**
(`evidence/v4/rust-tests-bounded.log`), release build clean; owner's exact
`scripts/build-virtiofs.sh` RC=0 (`evidence/v4/owner-build-bounded.log`).
New real-fcntl tests:

- `harddind_posix_table_cross_inode_deadlock_cycle_and_edges` (table level,
  two real host files, real `F_OFD_SETLK`): X/Y ABBA → immediate `EDEADLK`;
  repeated polls before the closing edge stay `EAGAIN`; after EDEADLK the
  winner never acquired X; holder unlock grants the pending cross-file wait on
  the next poll; cancel removes the edge (no phantom cycle); a three-file
  chain A→C→B→A is only a cycle when the closing waiter exists; flush on the
  other file leaves the waiter alive, flush on the waited file returns EINTR.
- `harddind_posix_two_file_deadlock_cycle_through_daemon` (full `PassthroughFs`,
  two looked-up inodes and four handles): A holds X waits Y; B holds Y waits
  X → `EDEADLK` immediately with real host fds confirming neither grant leaks;
  bounded non-cycle control polls `EAGAIN` then grants after unlock.
- Same-file cycle, noncycle, cancel/flush and all v1–v3 tests retained.

Native matrix: **27/27 pass** (`evidence/v4/posix-native-v12.json`), including
new `posix_two_file_deadlock` — two independent probe processes on two files
exercise the real kernel path: cross-file blocking waiter receives
`EDEADLK` (errno 35) immediately, and the one-way non-cycle wait grants on
unlock. The probe gained `other-open`/`other-close` and a `y` command suffix
routing to a second file descriptor (opened next to argv[1], so it works in
the container `/share` layout too).

Docker harness for the owner: `posix_two_file_deadlock_cycle` added to
`scripts/run-shared-locks.py` (two containers, two files, bounded EDEADLK
assertion with `receive(timeout=10)` and a non-cycle grant control); the same
probe commands drive it. Not run by this agent.

Full series 0001–0005 applies on the pristine pinned tree with `--fuzz=0`;
applied tree byte-identical to the tested tree.

## 4. Hashes (`evidence/v4/hashes.txt`)

- 0005 patch `338cb859eb95d19078bea1435d7411bb98c7a45e48bf9f02f315ff00c3463a70`
- virtiofsd binary `9c9703fe36b742cfdc26907d707e133e574ae14ab1b329139746ad521fdd74d9`
- series concatenation `2bc1f2786475550ab5b0c357aa71dc015b92cc5c2b5296566dd379dc043647a6`
- share_probe.c `a8a20ff0…`, probe binary `6eca5ae0…`, run-shared-locks.py
  `ca3379c7…`, run-posix-native.py `f5ab2c65…`

Owner guest command (I did not run it):

```sh
bash scripts/build-share-probe.sh <work>
make virtiofs RUST_TOOLCHAIN=/path/to/rust-1.98.1
python3 scripts/run-shared-locks.py <work> <kernel> <disk> --name shared-posix-v4 \
    --virtiofsd <work>/virtiofsd --posix-locks --inline-flock --functional-only
```

## 5. Still explicitly pending (not claimed fixed)

1. **Mixed host/guest deadlock cycles** — a wait edge whose other end is a
   host-process blocking lock cannot be observed from userspace (`F_OFD_SETLK`
   exposes no blocker identity); such waits resolve on host unlock. The
   implemented graph covers guest↔guest edges across any number of files only.
2. **Cross-PID-namespace GETLK attribution** — unchanged from v3: same
   namespace exact tgid, host holders 0, cross-container numbers opaque; needs
   a FUSE protocol identity extension.
3. **Post-`chmod` owner-fd reopen under a running guest** remains guest-only
   validation.
