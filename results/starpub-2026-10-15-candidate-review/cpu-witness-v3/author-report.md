# REPORT-v4 — owner-run-ready witness/correlator diagnostic (v3)

Scope: harness/instrumentation delivery only. No VM/GPU/native-busy/perf run
was started by this agent; the guest is owner-scheduled after mapping-v10.
Frozen v2 at `/root/harddind-rebuild/cpu-witness-owner-review-v2` is unchanged;
rejected-v1 is unchanged. New tree: `diag/v3/` with manifest
`diag/v3/MANIFEST-v3.json` (all hashes verified against the actual files,
including kernel/disk/frozen busy probe).

## 1. Preflight fixes to `run-cpu-witness.py`

1. **Compile/smoke path.** Removed the undefined `selftest` reference. Both
   the static build and the 1-capture smoke now run through the same recorded
   exact-process-group bounded runner (`run()`): distinct xb evidence files,
   `start_new_session`, SIGKILL-the-group + `cleanup_group` in `finally`, and
   a command row with `exit_code/timed_out/cleanup` even on failure; Popen
   spawn errors are caught and recorded (`exit_code 127`, `spawn_error`)
   instead of escaping.
2. **Unique poll labels.** `unique_label()` yields `base`, `base-1`, …; busy
   state polls use it, so repeated `run()` calls never collide on xb files.
3. **Sampler evidence is created at Popen** (`pid`, `pgid`, exact command,
   begin time) inside `cells[label]`, which exists before any later step can
   fail. Every exit path records end/elapsed, own CPU ticks (read while the
   process exists), forced-kill/timeout flags, and the full `cleanup_group`
   audit; planned-SIGTERM completion is gated on `clean_shutdown == true` and
   sampler summary `ok && termination_reason == signal`. No green cleanup is
   asserted without the process-group audit. Failed cells persist
   (`status: failed`, failure text, sampler record).
4. **Per-cell host snapshots.** `host_before` is captured inside `run_cell`
   immediately before each cell; `host_after` on completion/failure.
5. **Independent busy load per busy cell.** A fresh 0.5-quota 4-thread busy
   container is started for `separate-observer` and again for
   `same-observer`, so the fixed busy duration cannot expire between cells.
   Running is asserted before the observer (`start_busy`) and again
   immediately after it (`finish_busy` reads running + StartedAt/FinishedAt);
   if the load vanished, the cell aborts and no same/separate comparison is
   used for that run. The busy container is then wait-checked (exit 0),
   logged, removed.
6. **Finite non-nested locks.** `acquire_lock()` loops `LOCK_NB` with a
   bounded deadline; EX is taken only around compile/smoke and fully released
   before SH is ever acquired for the guest lifecycle. No nesting.
7. Affinity default remains 4 host CPUs (`0,1,2,3`), parsed strictly to
   ≤4 unique online CPUs; guest `--cpus` default 4. Nothing in the harness
   infers host-vs-UML cause.

## 2. v3 fail-open fixes to `host_correlate.py`

- `build_alignment` requires **exactly one start and one end** sync, each with
  a valid bracket; single-tag, duplicate-tag, unexpected-tag, missing-tag,
  and non-intersecting (skew) cases return `unavailable` with a reason.
- `analyze` now takes `--summary` (witness stderr) and `--expect-count`, and
  validates a strict schema: one summary with
  `requested == completed == expect-count` and `status == complete`; caps
  exactly `0..n-1`; five named, monotonic stages; raw `max1/stat/max2/proc`
  groups with errno. `attribution_valid` is true only when run integrity,
  witness completeness, and available alignment all hold; malformed/incomplete
  witness or missing summary yields ok=false/non-zero.
- `--uml-pid` uses a positive-integer parser (digits only; fractions, signs,
  exponent, 0 rejected).
- Identity/coverage/schedstat-unknown behavior from v2 is retained: raw
  counter regressions are never clamped, PID generations are never merged,
  zero/absent schedstat wait is never interpreted as "no scheduler wait".

## 3. Verification performed (bounded, under global EX; no guest)

- `gcc -O2 -Wall -Wextra -Werror -static` witness build clean.
- 23/23 analyzer fixture tests (`test_host_correlate_v3.py`): carried v2
  guarantees plus single/duplicate/missing/skew sync rejection, strict
  schema/count/summary negatives, strict PID parser, fail-closed analyze.
- 5/5 host harness tests (`test_run_cpu_witness.py`, self-builds the static
  witness): repeated unique labels write distinct evidence; a real
  witness+sampler cell through the actual planned SIGTERM path records pid/
  pgid/CPU/cleanup and produces strict correlation; an injected missing
  witness and an injected dead sampler both leave the sampler force-reaped
  with full audit and `status: failed`; a failing build keeps rc/cleanup rows.
- Live real-host planned flow (8 captures, 10 ms sampling): alignment
  available (~8 ms interval width), 35/40 stages covered, the 5 first-capture
  stages honestly `uncovered_pre` (no invented deltas), sampler measured at
  0.1 CPU-s / 73 samples with clean shutdown.

## 4. Owner command (serialized after mapping-v10)

```
python3 /root/harddind-rebuild/arkclaude/round12/cpu-capture-stall/diag/v3/run-cpu-witness.py \
  /root/harddind-rebuild/cpu-witness-v3-work cpu-witness-v3 \
  --kernel /root/harddind-rebuild/cpu-cpuset-kernel-v1 \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --repo /root/harddind-rebuild/cpu-observer-owner-v1/repo \
  --count 60 --interval-ms 100 --affinity 0,1,2,3 --cpus 4
```

Existing `results/<name>` or `instances/<name>` fails the run. Compile takes
EX separately; runtime holds SH. Outputs per cell: raw witness stdout/stderr,
sampler stdout/stderr, host samples + manifest, and strict
`correlation.json`; pre/post input hashes and normal guest stop/residual
audits are in the top-level report. The four cells are quiet-quota,
quiet-unlimited, separate-observer, same-observer (the last two each with
their own fresh busy load).

## 5. Interpretation boundary (unchanged)

This harness produces raw, identity-checked host scheduling evidence
alongside guest brackets. It assigns no cause: attribution rows exist only
when alignment and coverage are valid; otherwise stages are explicitly
unattributed. The actual v4 shares observations (1.89 s gaps; equal final
window 3.31 s) remain indeterminate, and no checker threshold/tolerance is
relaxed or modified by this work.

## 6. Hashes

```
84462bebed298a5fc085aca8e64a69e8cf9d14e6a79c6ccc033b120b7bd3f6c1  diag/v3/cpu_capture_witness.c   (unchanged from frozen v2)
6166f05a605f4e491a318b13aadd518a40dfce8714ece33743cb9e5da93b0a06  diag/v3/host_correlate.py
9df2c75468353c493d8c8b0669c0bcbb2865e78958f56ea91632d0d4a6eda606  diag/v3/run-cpu-witness.py
4d0f31774456b009d23a1a6a02e234ba8f462be079a236e792e15463392fb4e7  diag/v3/tests/test_host_correlate_v3.py
5e4fe1bcf7ce074fdd4acaf5feecae9d6711aead5ef5bd3dfea5f1264386fa5a  diag/v3/tests/test_run_cpu_witness.py
986012c74133153228b271fbece63063646d4c3f773fcef9ceb2d88fabc376ea  diag/v3/MANIFEST-v3.json
```
