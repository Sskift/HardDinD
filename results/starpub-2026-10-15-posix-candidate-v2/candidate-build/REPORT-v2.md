# 0005 shared POSIX locks — repair report v2 (staged)

Supersedes the rejected v1 candidate preserved immutably at
`evidence/v2/0005-rejected-v1.patch` (sha256
`0b69f4349b0c0de0b714c50036b2b199fd879861f3786d77a5e02f712881061f`,
the exact binary/patch the owner is comparing in
`/root/harddind-rebuild/posix-owner-v1` — that directory was not touched).

Patches 0001–0004 are unchanged. All builds used the pinned rustc 1.98.1
(`48a229cea…`), ≤4 jobs, under the global build lock. No guests, Docker, GPU or
performance runs were started by this agent.

## Stage 1 — P1 wire-range bugs (delivered in this update)

### Bug 1: `end=0` is one byte, not EOF

FUSE `lk.start`/`lk.end` are inclusive. Fixed `harddind_parse_posix_lock`
(`src/server.rs:93-151`): `(start=0,end=0)` now decodes to length **1**;
`(start>0,end=0)` is `EINVAL`. The old code's "legacy zero end" invention is
removed.

### Bug 2: `OFFSET_MAX` is `2^63-1`, not `2^62-1`

Read from the pinned kernel: `include/linux/fs.h:1334`
`#define OFFSET_MAX type_max(loff_t)` and `fs/locks.c:520-545`
(`flock64_to_posix_lock`) show `l_len=0 → fl_end=OFFSET_MAX` and bounded ends
forwarded verbatim; `fs/fuse/file.c:2502-2503` sends them directly.
`PosixLock::EOF` is now `i64::MAX as u64` (`src/filesystem.rs`). Legitimate
high ranges accepted; `end > 2^63-1`, inverted ranges and lengths that would
overflow signed `off_t` rejected. Note the kernel identity exploited/tested:
a 1-byte lock exactly at `OFFSET_MAX` and an unbounded lock starting there
encode identically on the wire and cover the same single byte.

### Tests added (real Rust backend, not just native userland)

- `server::harddind_parse_posix_lock_inclusive_ends_and_boundaries` — raw wire
  fields: `(0,0)→len1`, `(5,0)→EINVAL`, `(64,64)→len1`,
  `(…,2^63-1)→len0`, bounded requests above `2^62`, last byte, `2^63` and
  `u64::MAX` rejected, overflow, type/flag/truncation, sender pid passthrough.
- `passthrough::harddind_posix_one_byte_adjacent_large_offsets_and_eof` —
  end-to-end through `PassthroughFs`: acquire/GETLK/unlock of byte 0 only with
  adjacent exclusion; nonzero single byte; bounded locks at `2^62` and at
  `i64::MAX`; host conflict on the final byte; to-EOF from `2^62` with
  `F_OFD_GETLK` returning `l_len=0` → FUSE reply `end=2^63-1`.
- Harness: new owner-aligned case **`posix_one_byte_zero`** (same name/behavior
  as the owner's added Docker case) plus **`posix_large_offsets`** in both
  `scripts/run-shared-locks.py` (native + docker matrices) and
  `scripts/run-posix-native.py`. The driver also now accepts
  `--functional-only`, matching the owner's comparison driver semantics
  (`functional_complete`, skipped timing samples/inline-volume gate).

### Stage-1 verification

- `cargo test --release --locked harddind_`: **11/11 pass** (was 10);
  `cargo build --release`: clean (`evidence/v2/build-stage1.log`).
- Owner's exact `scripts/build-virtiofs.sh`: RC=0.
- Native matrix: **20/20 pass** (`evidence/v2/posix-native-v5.json`).
- Full series 0001–0005 applies on pristine pinned tree `--fuzz=0`; applied
  tree byte-identical to the tested tree.
- New hashes (`evidence/v2/stage1-hashes.txt`):
  - 0005 patch `0886ea7b1403af66af119c8db67569f4814b4b6e164a0752e21bc840865e77ce`
  - virtiofsd binary `04ead9768a8ab98462f5726cc55544ce60f7221723482148ea41f913f480cfdd`
  - patch series concatenation
    `058d2ff2883853d8b886dceff74ab3a22a7c10a4bc6132360deebc21359ca5fe`

## Stage 2 — residual semantics (in progress, recorded honestly)

Tracked from the owner's P1 list; nothing below is claimed complete until its
tests land and this file is updated:

1. **GETLK pid attribution (owner item 3).** v1 reports host pid for host
   holders and `u32::MAX` (-1) for guest holders, weakening the Docker check to
   `pid>=0`. Planned real fix: `fuse_lk_in.lk.pid` already carries the
   holder's tgid in the mount pid namespace (`fs/fuse/file.c:2542-2543`);
   record it per `(inode,owner)` at SETLK and return it for guest-held locks;
   return 0 for host-only holders (no guest-namespace translation exists, so a
   coincidental pid can never be misattributed). Same-namespace sibling
   process and cross-namespace (=0) coverage to be added on both sides.
2. **Deadlock detection (item 4).** Polling cannot provide `F_SETLKW`
   `EDEADLK`; planned guest-owner waiter graph with bounded two-owner cycle
   test; host cross-layer EDEADLK remains an explicit documented gap.
   Close-vs-wait teardown will get a real mutation test proving a flushed
   owner's pending wait cannot acquire/resurrect an fd.
3. **Coverage (item 5).** exec preservation probe case, cancellation/grant
   races for POSIX waits, O_RDONLY/O_WRONLY rights (guest already returns
   EBADF pre-daemon; owner-fd reopening will use the handle's access mode with
   per-mode fallback so a later permission change cannot kill an existing
   lock).

Nothing in stages 2 changes the stage-1 wire semantics; new failures or
not-tested results remain failures rather than silent fallbacks.
