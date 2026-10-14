# REPORT-v5 — FP oracle + harness hardening after OWNER-ACTUAL-MATRIX-V1

Date: 2026-10-14. Scope: probe/harness only. **No guest/GPU/performance run
and no kernel rebuild by me.** Kernel candidate and both built kernels are
the frozen v4 artifacts; `/root/harddind-rebuild/fp-owner-review-v4` and
`/root/harddind-rebuild/fp-matrix-owner-v1` were read, never modified. All
native compiles/models ran bounded (`timeout`) under the **exclusive**
global lock `flock -x /root/harddind-rebuild/kernel-build.lock`, compiler
jobs ≤ 4, with exact child cleanup.

## Independent-review corrections (read and accepted)

`round13/fp-v4-review/REPORT-CORRECTION.md` **retracts both blockers**:

* **C1 withdrawn.** task_struct is a compile-time sizeof that grows with the
  descriptor: DWARF-measured eager 2688 / lazy 2752, fp absolute offset
  2656 / 2696, leaving 32 B (eager) / 56 B (lazy) tail margin inside
  `sizeof(task_struct) + host_fp_size`. No slab change warranted; none made.
* **M2 withdrawn.** UML `copy_from_user` is the zero-padding
  `_inline_copy_from_user`; a failed guest rt_sigreturn leaves fp[] fully
  defined (user prefix + zero suffix) in pristine and patched kernels
  alike, and discard-before-overwrite is byte-neutral and required. No
  sigreturn change warranted; none made.

The patch is therefore byte-identical to frozen v4: sha
`59bd8b361ac69f36c8268aa3e4047c214ac3ecba0724b2cf56518a9a8281d2ca`.
Residual reviewer notes M3 (unreachable-branch hardening), L4 (reset
tidying), T1–T4 (evidence scope) stand as non-blocking; none is a current
bug and I made no kernel change for them.

## Owner actual matrix (diagnostic evidence, preserved)

`/root/harddind-rebuild/fp-matrix-owner-v1`: native **15/15** and all **12
guest cells** (kernels × cpus 1/4 × seccomp_threads 0/1 × lazyxstate 0/1)
**15/15, no skips**, 15 command groups clean, hashes stable, with probe
source `11e97893…`. I treat this as diagnostic only: performance/full
acceptance remain false pending owner scheduling.

## Oracle improvements made this round (src/fp_probe.c, now sha `4faa72ef…`)

1. **frame-churn asserts post-wake FP/SIMD.** Blocked threads now paint a
   *distinct* image each, park in a blocking read during the 200k-syscall
   storm, are woken **normally** (one byte per pipe), and in ordinary
   execution context verify byte-exact x87/SSE survival
   (`state_unchanged("blocked-wake")`) plus the wake byte/count; the main
   thread joins on a NULL return (corruption returns `(void*)1`) and closes
   the write ends. Cancellation is no longer used as an oracle (libc unwind
   would clobber SIMD registers).
2. **fork child asserts inherited bytes before writing.** The raw-clone
   child validates the parent's exact FP image **first**
   (`clone-child-inherited`, exit 21 on mismatch), only then paints its own
   distinct image and proves it across 100 syscalls (exit 20); the parent
   independently verifies its image is unaffected.
3. **Coredump NT_X86_XSTATE ELF-note — labelled gap, not faked.** No
   `ptrace-coredump` case exists; per instruction it is recorded here as a
   real coverage gap rather than asserted without proven note layout.
4. Carried over from the v4 follow-up and kept: entry-stop full-image
   GET/SET with real natural lengths (gpr=216, fx=512, xs=2696 on this
   host), six-register merge-only restore preserving live control flow,
   frozen-sample-1 assertion, fresh-baseline full-atomicity prefix checks,
   unique `fp-threads-1/-4`, and checked create/join/fork/wait everywhere.

## Harness improvements

* `fp-run-core.py` now: rejects **duplicate, unknown and malformed** JSON
  rows; requires a well-formed 64-hex in-guest probe hash (optionally an
  exact `--expect-probe-sha`); requires exactly-once
  BEGIN / `FP_PROBE_RC=0` / `END status=0` markers; enforces the exact
  fp-threads-1/4 cardinality; gates the verdict on kernel_unchanged; does
  deterministic process-group cleanup; and adds an offline
  `--validate-log-only` auditor (no guest launch). All gates were exercised
  with synthetic good/duplicate/unknown/malformed/skip/bad-rc/no-end logs
  (pass 0 / incomplete 2 / fail 1 verified). Expected case set is 15.
* `fp-native-tests.sh`: switched to one bounded **exclusive** flock (no
  nested flock anywhere), `-j4`, per-mode `timeout`, an EXIT trap that
  kills all spawned jobs, and builds the probe with the required `-O2`
  (the tracee sampling block is unsatisfiable at -O0).
* `fp-build-kernel.sh`: verified/idempotent tree preparation already
  landed last round (reuse on identity, exit 3 on drift unless
  FP_REFRESH_TREES=1); no kernel rebuild this round.

## Observed baseline ABI difference (not equivalence)

Native ptrace short (prefix) FX/XSTATE SET is **rejected atomically** on
this host (`EINVAL`/`EFAULT`, full image byte-unchanged); the owner's guest
matrix accepts the same 15 cases. The probe deliberately treats both as
valid and proves exact retained bytes / atomic reject against a fresh
baseline (`ptrace-prefix-semantics`), so it passes under either kernel ABI;
it does **not** claim the two are equivalent. A reviewer is investigating.

## Bounded native result this round (diagnostic)

`timeout 240 bash scripts/fp-native-tests.sh` under EX lock → **rc 0**.
Models: all modes normal + ASAN clean (owner-repro, null-drop strict put,
3000 reuse, teardown, 4 interleavings, 4000 exit-vs-takeover, 4000
materialise/discard-vs-takeover, feature-off accounting, ~460k lazy +
~1.02M eager rounds, born==freed). Probe: **15/15 pass, 0 fail, exit 0**;
no leftover model/probe processes after the run. 49 python checks pass.
Raw log: `.work/native-results/native-v5.log`.

## Artifacts (sha256)

| file | sha256 |
|---|---|
| kernel patch (frozen v4, unchanged) | `59bd8b361ac69f36c8268aa3e4047c214ac3ecba0724b2cf56518a9a8281d2ca` |
| src/fp_probe.c (new oracles) | `4faa72efd038d15c7e3246609769ac305dfea4cc1ad932485ca339620b62d648` |
| src/fp_model.c (unchanged rev4) | `65b1a2dbafa93ac818a928a863c986d87a28fa06929f9238e957611dba77ec0b` |
| scripts/fp-run-core.py | `bc6ad1f7f1351ab49d723cc7a738f00872bdecb9c30521dc13c5108fe6c16849` |
| scripts/fp-native-tests.sh | `a0c8acf5eed2a648fcd4ada660788ccc367f0b6f9b68f614e22d8b9f33b70665` |
| scripts/fp-build-kernel.sh (unchanged since v4 follow-up) | `1a1bc99edd4762906abd6e6493ff5a1ad27fb428008df17cacdb4ac1872a29a3` |
| built native probe binary | `483b66e64a392b3c6947009cb9440c696022036c12d7141489c32fe883a02b8c` |

Failure history: previous candidate defects and the withdrawn C1/M2 claims
are preserved in `OWNER-*.md`, `docs/failed-evidence/`,
`/root/harddind-rebuild/fp-owner-lifetime-v1`,
`owner-review-v2/hung-model.json`, and `round13/fp-v4-review/REPORT*.md`.
