# REPORT-v7 — raw PTRACE_SETREGSET observation path corrected

Date: 2026-10-15. Scope: probe-only ABI evidence fix. **No kernel change or
rebuild, no VM/GPU/benchmark.** Kernel patch remains frozen v4
`59bd8b36…`; `/root/harddind-rebuild/fp-abi-owner-review-v1` (owner's
frozen v6 evidence) was not modified. All native work bounded (`timeout`,
suite wrapped in 180 s) under the **exclusive** global lock
`flock -x /root/harddind-rebuild/kernel-build.lock`, compiler jobs ≤ 4,
exact child cleanup.

## Explicit retractions of v6 claims

**RETRACTED 1 — `post_iov_len = 0` on rejected SET.** In v6 the prefix
probe used `regset_io(..., &post)` with `post` initialised to 0, and that
helper only assigns `*used = iov.iov_len` on the success path — on error it
returns `-errno` before the assignment. So every rejected record reported
`post_iov=0` from the **untouched local sentinel**, not from the kernel.
The v6 sentence "post_iov after a failed call is observed 0 on this host"
in REPORT-v6, and all `iov0` tokens in the v6 records, are wrong and
withdrawn.

**RETRACTED 2 — normalized helper return presented as raw ptrace rc.** v6
recorded `rc=-22 / rc=-14`, which is the helper's normalized `-errno`, not
the syscall return. The raw `ptrace(PTRACE_SETREGSET, …)` return on a
rejected request is **-1** (with `errno=22/14`). Any v6 reading that used
`rc=-22/-14` as the kernel's return value is withdrawn.

**Observed correction (this host, 5.15, raw direct calls):** on a rejected
short SET the raw return is **-1**, errno is **EINVAL(22)** for FX28/32/256
and XS172 and **EFAULT(14)** for XS176/520, and the kernel leaves
`iovec.iov_len` **unchanged**, i.e. equal to the requested length
(28/32/256/172/176/520). No inference of "kernel writes no iov" is made; the
value is read after every call.

## What changed

1. **`prefix_one()` in src/fp_probe.c now calls `ptrace()` directly** for
   the SET: `iovec.iov_len` is initialised to the requested length, the raw
   `long` return is captured, `errno` is read immediately, and
   `iov.iov_len` is snapshotted unconditionally after the call. The record
   field is renamed `rc` → **`raw_rc`** (values 0 / -1); `post_iov_len` is
   the genuine post-call value. GETs still use the helper (full GETs succeed
   and read iov_len correctly). The accepted/retained and atomic-reject
   full-image byte proofs are unchanged.
2. **New device-free assertion `tests/ptrace-raw-regset.c`** (static,
   self-contained traced child, no /dev or VM). It issues the six raw short
   SETs and asserts `raw_rc == -1` AND `post_iov_len == requested_len`; if
   either differs on a host it exits non-zero and prints the observed
   values rather than relying on a sentinel. Wired into
   `scripts/fp-native-tests.sh` (bounded under EX).
3. **MXCSR label corrected.** The prefix-probe value `0x1fa0` is
   `0x1f80 | 0x20` — the **PE (precision exception) sticky status flag,
   bit 5**, not DAZ. DAZ is **0x40 (bit 6)**. The three prefix values are
   now documented accurately as distinct legal exception **status** flags:
   `0x1fa0` +PE (0x20), `0x1f90` +OE (0x10), `0x1f81` +IE (0x01). The
   separate ptrace sample1 edit genuinely uses DAZ (`0x1fc0 = 0x1f80|0x40`)
   and its "DAZ" label is correct and unchanged. Invalid reserved/mask-bit
   MXCSR and unsupported-xfeature images remain an untriggered pre-existing
   UML gap.

## Bounded native records (corrected, this host)

From both the standalone `ptrace-raw-regset` and the probe's six records:

```
tag    regset        req  raw_rc errno post_iov outcome
FX28   NT_PRFPREG      28  -1     22    28       atomic-reject  (alignment gate)
FX32   NT_PRFPREG      32  -1     22    32       atomic-reject  (aligned/full-policy)
FX256  NT_PRFPREG     256  -1     22   256       atomic-reject  (aligned/full-policy)
XS172  NT_X86_XSTATE  172  -1     22   172       atomic-reject  (alignment gate)
XS176  NT_X86_XSTATE  176  -1     14   176       atomic-reject  (aligned/max_size)
XS520  NT_X86_XSTATE  520  -1     14   520       atomic-reject  (aligned/max_size)
```

`ptrace-raw-regset OK: rejected SET raw_rc=-1 and post_iov_len==requested_len
for all 6 lengths`. The guest-UML divergence (aligned FX/XS prefixes
accepted with exact retention; unaligned still gated) stands as described in
the reviewer's REPORT-MATRIX-V1 §2 — pre-existing upstream UML copyin
semantics, not a parity claim and not a 0018 regression.

## Result (diagnostic only)

`TEST_TIMEOUT=60 ASAN_TIMEOUT=60 timeout 180 bash scripts/fp-native-tests.sh`
under EX lock → **rc 0**. Probe **15/15 distinct cases pass, exit 0** with
the corrected raw records; standalone raw-regset assertion passes; all
model modes normal+ASAN clean (6 `failures=0` schedules, born==freed); 49
python checks pass; no leftover processes. Raw log:
`.work/native-results/native-v7.log`; JSON `.work/native/probe.json`.

**Owner matrix provenance:** the owner kernel matrix v3 was run against the
frozen earlier probe/kernel sources; it is NOT relabelled as having used v6
or v7 source. These v6/v7 probe-only changes do not alter the kernel and
await any owner decision on re-running a matrix; performance/full
acceptance remain false.

## Artifacts (sha256)

| file | sha256 |
|---|---|
| kernel patch (frozen v4, unchanged) | `59bd8b361ac69f36c8268aa3e4047c214ac3ecba0724b2cf56518a9a8281d2ca` |
| src/fp_probe.c | `d9774a5238af4736490063c582faa8e1bc613c98507bc4e5ed2f6102b1550789` |
| tests/ptrace-raw-regset.c (new) | `0151080857bb42b8a18f09c14270e46e04ba2fd6b6f7a4b632161d477db33430` |
| scripts/fp-native-tests.sh | `375ffdd3be70b6b7ee3482fe099033488039e30fe842aa2f9be2080da9deb306` |
| src/fp_model.c (unchanged) | `65b1a2dbafa93ac818a928a863c986d87a28fa06929f9238e957611dba77ec0b` |
| scripts/fp-run-core.py (unchanged) | `bc6ad1f7f1351ab49d723cc7a738f00872bdecb9c30521dc13c5108fe6c16849` |

Prior failure/retraction history: v6 probe claims live only in this
retraction and the owner-frozen `fp-abi-owner-review-v1`; earlier
C1/M2 reviewer claims remain withdrawn per
`round13/fp-v4-review/REPORT-CORRECTION.md`.
