# 0005 shared POSIX locks — repair report v5 (atomicity + verifier scope + harness truth)

Supersedes v4, frozen by the owner at
`/root/harddind-rebuild/posix-owner-review-v4` (patch
`338cb859…`, not modified). Addresses the four points in
`OWNER-V4-ATOMICITY-REVIEW.md`. Patches 0001–0004 and owner main were not
touched; no guest/GPU/performance runtime used. Every build/test bounded under
the global EX lock (normal wait on the owner queue), ≤4 jobs, GNU
`timeout --kill-after=30s` (tests 180s, build 600s). Filtered Rust suite runs
in ~0.2–0.3 s.

## 1. Syscall ↔ shadow interval state is now one critical section

**Gap fixed**: `posix_attempt` issued the host fcntl inside
`with_owner_file` (take+release per-inode serialize) and then called a
separate `note_grant` that reacquired it. A same-owner unlock on another
worker thread could complete between the two; the stale grant note then
reinserted a phantom interval. The blocking path already mutated the shadow
inside its section and was then re-mutated by `note_grant`.

**Fix** (`src/passthrough/posix_locks.rs`):
- `fcntl_and_shadow_locked` performs `F_OFD_SETLK` and the shadow
  insert/cut under ONE held `serialize` guard (guest-pid store included).
- One-shot `attempt_once` and blocking `wait_attempt` both call it inside
  their locked section; the post-hoc `note_grant` and its public API are
  deleted. Blocking `F_UNLCK` is now handled correctly (type/l_len honored).
- Lock order documented: per-inode `serialize` → global `waiters` →
  owner `intervals` (leaf).

**Deterministic real-table/real-fcntl regression**
`harddind_posix_syscall_shadow_atomicity_race`: two same-owner threads
barrier-released into grant vs unlock over the same range, 400 bounded
rounds; after each join an authoritative host `F_OFD_GETLK` is compared with
the table shadow (host free ⇒ attribute 0; host held ⇒ attribute pid 71).
No timeout/map-only assertion. **Proof it fails on the old code**: the v4
seam was recreated in a scratch build (two serialize sections + yield) and
the test failed **5/5** runs with shadow/host divergence; with the fix it
passes and is stable across 10 repeated suite runs
(`evidence/v5/seam-regression-proof.txt`).

## 2. Cycle verification locked-scope corrected

**Gap fixed**: Phase 3 locked only the candidate inodes from the initial
snapshot but re-ran DFS over the whole graph, so a different cycle through
unlocked inodes could have been reported.

**Fix**: verification is a deterministic expansion loop. Each pass acquires
serialize guards for a set of inodes in ascending `Inode` order, then the
global waiter registry, and `reach_with_locked` traverses only waiters whose
inode is one of those **same locked Arc sets** (grants frozen); an edge on an
unlocked inode returns `NeedExpand(Vec<Inode>)`, the set grows and the pass
repeats (bounded; each iteration adds ≥1 inode). Only a cycle fully covered
by locked sets yields `EDEADLK`; a changed/disappeared cycle yields
`EAGAIN`; forgetting an inode converges safely. Global-waiters-alone is not
claimed to freeze grants — every grant mutation requires the per-inode
serialize, all of which are held on the cycle.

**Deterministic regression**
`harddind_posix_cycle_verifier_respects_locked_scope` drives the real
verifier with constructed waiters: `{X}` locked where A's edge lives on Y
⇒ `NeedExpand([Y])`, never Cycle/NoCycle; `{X,Y}` ⇒ Cycle; edge removed ⇒
NoCycle. The existing two-inode/four-handle daemon EDEADLK test and the
table-level cross-file ABBA/noncycle/cancel/flush tests remain.

## 3. Harness false claims removed

- **GETLK host pid** (`posix_getlk_host_pid_zero`): native now asserts the
  holder's real tgid (`os.getpid()`); Docker asserts 0 (cross pid-namespace
  translation). The unconditional 0 is gone.
- **Cancel/grant race** (`posix_cancel_grant_race`): replaced the random
  8-iteration loop with two deterministic outcomes. (a) INTERRUPT: 5 ms timer
  fires while the outer holder still holds; EINTR is asserted and **no peer
  unlock is sent** — a SECOND guest process (`verifier` Peer, since fds of
  one process share POSIX locks and can never prove exclusion) is denied,
  then acquires only after the outer release. (b) GRANT: outer unlocks well
  before the timer; the granted peer excludes the second process until the
  peer explicitly unlocks. Same fix applied in `run-posix-native.py`.
- Also fixed a real probe bug found by this work: `posix_timed_wait` blocked
  SIGALRM during `F_SETLKW`, so the signal stayed pending and never
  interrupted the syscall (interrupt timing was effectively untested); it is
  now unblocked during the wait with pending-timer drain afterward.
- Limitations text updated to the actual scope (exact tgid same namespace;
  0 cross-namespace/host; guest-only deadlock detection).

## 4. Bounded raw results

- Rust `harddind_` suite: **18/18 pass in ~0.22 s**
  (`evidence/v5/rust-tests-bounded.log`; 2 new tests this round); release
  build clean.
- Owner's exact `scripts/build-virtiofs.sh` RC=0
  (`evidence/v5/owner-build-bounded.log`); daemon smoke OK.
- Native matrix: **27/27** (`evidence/v5/posix-native-v14.json`) including
  the strengthened deterministic interrupt/grant outcomes.
- Full 0001–0005 series applies on pristine pinned tree with `--fuzz=0` to a
  byte-identical tree.
- Hashes (`evidence/v5/stage5-hashes.txt`):
  - 0005 patch `a7ffeca5fd5b847879d92b0f160035c88eee5440d6c95dd9b62b8c44ad51c37c`
  - virtiofsd binary `13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`
  - series concatenation `0fd883e20fd55d1555f6e4eed4246c1da4dddce54d84fc5eda5426c0d9cbca1d`
  - share_probe.c `fe8127b2…`, probe `b962401b…`, run-shared-locks.py
    `85fc3184…`, run-posix-native.py `9e269a15…`

Owner guest command (not run by this agent):

```sh
bash scripts/build-share-probe.sh <work>
make virtiofs RUST_TOOLCHAIN=/path/to/rust-1.98.1
python3 scripts/run-shared-locks.py <work> <kernel> <disk> --name shared-posix-v5 \
    --virtiofsd <work>/virtiofsd --posix-locks --inline-flock --functional-only
```

## 5. Still explicitly pending (unchanged, not claimed)

1. Mixed host/guest blocking deadlock cycles (no blocker identity from
   userspace polling).
2. Cross-PID-namespace GETLK attribution beyond same-namespace exact tgid /
   host=0 (needs a FUSE protocol identity extension).
3. Post-`chmod` owner-fd reopen under a running guest: implemented with
   exact-mode fallback, validated only natively/at unit level.
