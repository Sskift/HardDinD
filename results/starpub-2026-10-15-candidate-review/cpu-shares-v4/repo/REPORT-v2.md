# Docker `--cpu-shares` contention experiment — v2 report (post owner review)

Date: 2026-10-14 (UTC)
Status: **v2 code complete, statically and synthetically validated; runtime remains owner-scheduled. No runtime PASS is claimed.**
Supersedes: rejected `REPORT.md` (v1). The v1 report is retained for the record; this file is separate as requested.
Review inputs: `owner-review-v1/counterexamples.json` (sha256
`f00c7dbfd72f5afbc3bcdd3fa94636e12d3ef1e735a11a85425a2128859986ed`),
frozen checker `e3368c83…`, and the owner's `reproduce.py` harness
(`reproduce-status.json`: exit 0 confirms false passes, not acceptance).

## 1. Owner findings and how v2 closes each

The owner reproducibly obtained `verdict=pass` from the frozen v1 checker after
deleting evidence. I re-ran the owner's exact mutations against the frozen
code and confirmed all four false passes, then confirmed v2 fails closed on
each (see §6).

| Attack (owner case) | v1 defect | v2 behavior |
|---|---|---|
| delete `commands` + producer `required_commands` | ledger trusted a producer-supplied allowlist; absent evidence iterated as empty | checker owns the canonical 46-label `MANDATORY_LABELS`; absent/empty ledger is FAIL. Unknown labels, duplicate labels and malformed rows (`exit_code`, `timed_out`, `cleanup.clean_shutdown`) are FAIL; `poll-*` readiness probes are the only repeatable labels and remain fully timeout/group audited |
| delete all `hashes_before/after` (`missing_input_provenance`) | `all()` over empty maps was vacuously true | mandatory `INPUT_ROLES` (kernel, disk, probe_source, probe_binary, runner, checker, harddind, guest_cleanup) each require a non-empty 64-hex digest equal before/after; unknown/duplicate roles and changed digests FAIL |
| delete every snapshot `cpuset.cpus.effective` (`missing_midrun_cpuset`) | `if errno == 0` skipped a missing key; other snapshot fields never audited | `validate_stage_snapshot` requires, per bracket/stage snapshot, `cpu.max` errno 0 & unlimited, `cpu.stat` present, `cpu.weight` numeric, and effective cpuset errno 0 exactly equal to the shared CPU; same per periodic sample (`weight_errno`, `cpuset_eff_errno`, `max_errno`, `stat_errno` must all be 0, integer weight 1..10000) |
| delete `leftovers_stdout` (`missing_leftovers_inventory`) | `evidence.get(..., '')` defaulted absence to an empty inventory | the evaluator passes the raw value with **no default**; a missing key/`None` is FAIL, `''` (an executed, present, empty inventory) passes |

Additional fail-closed hardening: unknown/malformed update records, duplicate
update records, duplicate/unknown input roles, absent daemon identity, absent
final guest state, non-`contend` streams (legacy `spin` without per-sample
weights), missing check rows and non-empty runner error lists all force FAIL.
Status separation is preserved: contradictory evidence = **fail**,
noisy/short/starved measurement = **indeterminate**, never-measured phases =
**not_tested**.

## 2. Timing defect fix: continuous in-guest weight timeline + brackets

v1 ended a phase at the *post-update exec snapshot* minus a fixed 0.6 s guard.
`docker exec`/inspect latency can be multi-second, so the actual weight change
could have landed long before the snapshot; the scored interval contained an
unbounded unknown-weight region. v2 removes the inference entirely:

* The new own probe mode `contend` (§3) writes the live `cpu.weight`,
  `weight_errno`, `cpuset_eff_errno` and the sampler thread's own
  `CLOCK_THREAD_CPUTIME_ID` on **every ~100 ms sample**, in the same
  `read_cg()` instant as `cpu.stat`, timestamped in guest `CLOCK_MONOTONIC`.
* The checker derives maximal same-weight runs per container, splitting a run
  on any coverage hole > `MAX_GAP_S` (0.5 s). The protocol requires exactly
  A:[heavy, light] and B:[light, heavy, light], using exactly two distinct
  weight values with the planned ordering.
* Each phase interval is the **intersection of both containers' actual
  matching runs** — C0 `[max(firsts)+0.5 s start trim, min(lasts)]`, C1 and
  C2 analogously with no fixed guard. Everything between the last old-weight
  sample and the first new-weight sample is excluded, regardless of how many
  seconds the live update (or its observation) took. Phase intervals must be
  chronologically disjoint (overlap across a boundary is FAIL).
* Per-container rates come only from samples inside the joint interval whose
  observed weight equals that phase's weight. The selected counter-capture
  endpoints of A and B are required to be the nearest actual periodic samples;
  their skew must be ≤ `ENDPOINT_SKEW_S = 0.5 s` (with a 100 ms shared
  cadence, healthy endpoints are ≤ 0.1 s apart — the bound exists for
  tampered/irregular streams).
* Explicit pre/post `docker exec snapshot` brackets still frame every issued
  update (`preupd-*`, `postupd-*` labels): pre must show the old weight within
  the old run (≤ 0.5 s bracket bound), post the new weight within the new run,
  pre before post. They corroborate the timeline and record update latency;
  the timeline remains the scoring authority.
* Windows need ≥ 4.0 s and ≥ 20 samples per container; shorter surviving
  phases are INDETERMINATE, never scored. Excluded gap sizes and endpoint
  skews are written into the `capture_protocol` evidence.

The strong fixture `test_three_second_delay_is_excluded_and_still_scored`
delays B's real weight change by ~3 s: the 3.15 s transient is excluded from
both phases, both phases still pass on clean interiors (proving delay does
not cause misattribution), and a larger delay that starves a phase below
4 s yields INDETERMINATE (`test_delay_that_starves_a_window_cannot_pass`).
`test_non_overlapping_heavy_runs_cannot_pass` places B's heavy run in a
distant time band; `test_coverage_hole_splits_run_and_fails` removes 2 s of
samples. None can pass.

## 3. Own probe copy (new file; seed probe untouched)

`src/cpu_share_probe.c` (sha256
`64945a2ecbb14e53ed3fb300d92897cae2e48c1d95360d1c69ac8442e1dbb6b1`, 458
lines) was written from the **currently released** probe
`/root/harddind-rebuild/cpu-limits-owner-v3/repo/src/cpu_limit_probe.c`
(sha256 `7dd2d82dc1e0d9c7a378de0c8ee5eabcb1f5a86d78ab1c151e5fc1da7779b2de`,
main ff6d767 lineage — adds `sampler_cpu_ns`/`load_deadline_ns`). The seed
`src/cpu_limit_probe.c` (f383, sha256
`f6791d6bb1ac050933051c6fc1857bbd8abae8c206cee509eb5a44ee9fc87a82`) and all
common files are unmodified (mtimes verified at/before 19:30).

* Existing modes `selftest/info/snapshot/spin` are behaviorally preserved;
  `spin` output is byte-compatible with the released probe (same
  `sampler_cpu_ns`/`load_deadline_ns` accounting). `selftest` gains one
  additive `parse_weight` check.
* New dedicated mode `contend` shares the spin implementation via one internal
  function (`run_spin_like(..., contend)`): same limits
  (100..120000 ms duration, 1..64 threads), same worker loop, same start/end
  events; only the sample line gains the co-located weight timeline fields.
* The checker supports released-probe sampler accounting: whole-run thread
  rate = `worker_cpu_ns + (end.sampler_cpu_ns - start.sampler_cpu_ns)` over
  wall, compared with process clock and `cpu.stat` (basis
  `workers_plus_sampler` recorded); per-window rates use proc/stat samples.
  Samples after `load_deadline_ns` are excluded from busy windows.
* **Pending integration (documented, not copied):** round10
  `cpu-sampler-repair` (status `running` at time of writing) is adding
  persistent-fd double reads, bracketed captures and time guards. I did not
  touch or copy that in-flight work. When it merges, the share checker's
  per-sample single-read co-location can be upgraded to the bracketed
  double-read contract by widening `validate_contend_stream` to the new sample
  schema; the timeline/run/window logic above is independent of that read
  mechanism. The own probe should then be reconciled/dropped in favor of the
  merged released probe.

Build evidence: compiled once under the mandated global EX lock
(`flock -x /root/harddind-rebuild/kernel-build.lock gcc -O2 -Wall -Wextra
-Werror -static -pthread`, bounded 55 s, single gcc, clean log) → exit 0;
`selftest` reports all parsers `pass`. A second lock attempt during final
audit blocked on owner-held lock activity and was killed rather than queued;
it was redundant because the source has not changed since the successful
locked build (source mtime 20:25:29 precedes the binary 20:25:44). Only
`selftest`/`info`/`snapshot` were executed on the host; **no `contend`/busy
spin, guest, GPU or native load was ever run by this agent**. (The host build
box itself carries a `cpu.max` quota `3599999 100000`, which is why the
checker's strict `max` requirement correctly rejects host snapshots —
expected, and irrelevant inside the guest.)

## 4. Expected fractions come from observed weights, with mapping identity

Docker/Moby maps `--cpu-shares` to cgroup v2 `cpu.weight` **nonlinearly**
(roughly `1 + (shares-2)*9999/26214`); shares 1024/512 are observed as kernel
weights 39/20 on this stack. v2 never computes expectations from shares. The
continuous timeline must contain exactly two values `wlo < whi`; every
stage/inspect pair must agree on one consistent mapping table
{512: wlo, 1024: whi}; timeline run weights must equal the snapshot mapping;
spin start/end weights must agree with their planned stages. Expected
fractions are `whi/(whi+wlo)` (0.6610 for 39/20), `wlo/(whi+wlo)` (0.3390),
and 0.5 equal — with the original tolerances **unchanged**: ±0.09 weighted
stages, ±0.08 equal control, proc/stat fraction agreement 0.05, ordering
margin 0.05, counter agreement max(0.03 cores, 5%), whole-run agreement 0.08,
saturation band [0.85, 1.15] cores, starvation floors 0.10 cores / 0.12
fraction. There is still **no workload-throughput acceptance criterion**.
The mapping, its per-occurrence pairs, daemon `ServerVersion` and cgroup
version are recorded in the `weight_mapping` evidence.

## 5. Files (v2 adds one probe; rewrites three v1 deliverables)

| File | sha256 | Lines |
|---|---|---|
| `src/cpu_share_probe.c` (new own probe copy) | `64945a2ecbb14e53ed3fb300d92897cae2e48c1d95360d1c69ac8442e1dbb6b1` | 458 |
| `scripts/cpu_share_checks.py` (v2 rewrite) | `03cd2bc6d7329581b7c06094a29ebf736cc800f705229da784999c55b86c2a17` | 1025 |
| `scripts/run-cpu-shares.py` (v2 rewrite) | `a5fa168caa739495d828259f41524eb68774e3e9a99a55f43ed65a96b6bef881` | 457 |
| `tests/test_cpu_share_checks.py` (v2 rewrite, 60 tests) | `69e3971d5d72d069109394f197599b60dc882d0c26c75064ef785cf005dbb994` | 745 |
| `REPORT.md` (rejected v1, retained) | `4b63f5334aef77ed91dde6aeafea1ba66e958ff21a8aa67cb3a31c161adeae29` | — |
| `owner-review-v1/counterexamples.json` (owner, read-only) | `f00c7dbfd72f5afbc3bcdd3fa94636e12d3ef1e735a11a85425a2128859986ed` | — |

(This file's own hash is necessarily excluded from its table.) Seed/common
files unchanged: `src/cpu_limit_probe.c`, `scripts/harddind.py`,
`scripts/guest_cleanup.py`, `scripts/run-cpu-limits.py`,
`scripts/cpu_limit_checks.py`, `tests/test_cpu_limit_checks.py` and
everything under `owner-review-v1/` and round10.

## 6. Validation performed by this agent (static/synthetic only)

Environment: Python 3.10.12, gcc 11.4.0, Linux 5.15 host. Bounded ≤ 60 s,
single test process; no guest/VM/GPU/busy spin/benchmark.

1. **v1 false-pass reproduction** against the frozen owner checker:
   baseline, no-ledger, empty-hashes, no-snapshot-cpuset, no-leftovers all
   returned `pass` — matching `counterexamples.json`.
2. **Same attacks against v2 all return `fail`** (adversarial sweep also
   covering empty command/input/update lists, empty streams, empty daemon and
   empty final state).
3. **v2 unit tests**: `timeout 60 python3 tests/test_cpu_share_checks.py` →
   **60 tests, OK**. Coverage includes: baseline pass; weights-not-shares
   fractions; 3 s delayed update excluded yet scored; delay-starved phase
   INDETERMINATE; post-bracket contradicting the timeline FAIL; all four owner
   deletions; present-empty vs absent leftovers; legacy spin stream FAIL;
   unknown/duplicate/missing/malformed command rows; poll timeout and dirty
   optional groups; unknown/duplicate/changed/malformed input roles;
   snapshot weight/stat/max/cpuset errors; missing per-sample weight; missing
   brackets; opposite allocation; equal-control skew; mapping inconsistency;
   inspect drift; quota present; three-weight timeline; weak ordering margin;
   distant non-overlapping heavy runs; 2 s coverage hole; actual-sample window
   intersection geometry; 0.8 s startup stagger; insufficient load; >1 core;
   starvation; proc/stat disagreement; wrong guest size; cgroup v1; residency
   escape; identity change; nonzero probe exit; leftover IDs; forced shutdown;
   missing/failed updates; missing check row; runner errors; wholly empty
   evidence.
4. **Seed regression**: `timeout 60 python3 tests/test_cpu_limit_checks.py` →
   **31 tests, OK**. Combined discovery
   `python3 -m unittest discover -s tests -p 'test_cpu_*.py'` → **91 tests,
   OK**.
5. **Real released-shape probe cross-check**: checker parses the actual probe
   `snapshot`/`info` output; host cgroup evidence correctly rejected (host
   quota/cpuset), fixture-shaped events validate at weight 100.
6. **Protocol consistency**: static cross-check proves every one of the
   checker's 46 mandatory labels is emitted by the runner exactly in the
   required protocol sequence (build → start → stage 0 → boundary 1 brackets
   for A and B → stage 1 → boundary 2 bracket for B → stage 2 → wait/logs/rm
   → leftovers → stop), with only `prerm-*`/`cleanup-*`/`final-stop`/`poll-*`
   optional.
7. **Runner failure path** (throwaway WORK, fake failing `gcc`, no guest):
   exit 1, report always persisted and re-parsable, `fail` verdict, phases
   `not_tested`, ledger/inputs/leftovers/lifecycle `fail`, raw stderr kept in
   `build-share-probe.stderr`; artifacts removed.
8. Runner argparse rejects bad name, non-4 guests, out-of-range shared CPU and
   bad timing before any side effect; `--help` works.

## 7. Owner execution (unchanged interface, runtime not run by this agent)

```
python3 scripts/run-cpu-shares.py <WORK> \
  --kernel <kernel> --disk <runtime.ext4> --name <immutable-name> \
  [--cpus 4] [--shared-cpu 2] [--threads 4] [--phase-ms 9000] [--sample-ms 100]
```

Produces `<WORK>/results/<name>/report.json` (schema_version 2) with per-command
stdout/stderr, pre/post bracket events, the full weight-timeline-bearing
contend JSONL from both containers, 17 named check rows, input role hashes,
excluded-gap and endpoint-skew evidence. Exit codes: 0 pass / 1 fail /
2 indeterminate-or-not_tested. Bounded guest busy spin ~32 s at defaults;
schedule under the owner queue and the global kernel-build.lock protocol.

## 8. Honest pending-runtime boundary

Not observed yet: real Docker conversion latency and the live 39/20 mapping
inside the UML guest, actual allocation fractions under contention, whether
phase interiors comfortably clear 4 s at the default 9 s spacing (rerun with
larger `--phase-ms`, max 20000, if INDETERMINATE), and clean guest lifecycle
with the new image. No runtime acceptance is claimed here. After round10 merges
its sampler repairs, the probe schema and checker should be reconciled per
§3; until then the persistent-fd/bracketed-read hardening from round10 is
documented as pending, while the present single-read co-located timeline
already gives fail-closed bounded capture by construction.
