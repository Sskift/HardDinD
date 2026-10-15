# Docker `--cpu-shares` experiment — v6 report (evaluator purity + read-only v6 analysis)

Date: 2026-10-15 (UTC). Supersedes v5. Source/checker and Python tests only;
no guests, no native benchmark/GPU, no functional criterion widened. Owner
reruns when needed.

## 1. What the owner found in actual v6

Immutable: `/root/harddind-rebuild/cpu-shares-owner-v6/` (106 tests no
skips; 46 commands journaled; 51 groups empty; normal shutdown). The run
verdict is **INDETERMINATE** and that is preserved: earlier phases contain
gaps; the equal-weight phase passes; ordering therefore not_tested.

New replay defect: `checks.evaluate()` **mutated its input** — it replaced
each update's raw `pre`/`post` snapshot in place with a normalized dict and
added `stage_snapshots` to containers. The serialized `report['evidence']`
thus cannot re-evaluate: replaying it yields `probe_streams = fail` ("pre
bracket is not a snapshot event") instead of reproducing the report's own 17
rows.

## 2. Exact purity repair — `scripts/cpu_share_checks.py` (`4eed8d67…`)

`evaluate()` is now a **pure function**. At entry it takes private deep copies
(JSON round-trip) of `params`, `daemon`, `containers` and `updates`; every
normalization (stage snapshots, bracket validation) happens on those copies.
The caller's evidence is never written. No classification threshold or
verdict logic changed — only mutation was removed. Verified:

* frozen v6 stored evidence direct replay still **fails probe_streams** —
  the historical defect is documented and not erased;
* a fresh/unmutated evidence replayed after the fix reproduces the report;
* `evaluate()` twice returns identical rows and the input is byte-identical
  before/after.

The live runner therefore now serializes raw (un-normalized) bracket
snapshots, so future reports re-evaluate to their own verdict. Runner/probe
files are otherwise unchanged this round.

## 3. Actual-v6 rehydration helper (reconstruction, not a rerun)

`scripts/rehydrate_shares_v6.py` (`ce5c26a1…`) rebuilds evaluator input from
the **original raw captures already on disk** — `logs-a/b` contend JSONL,
raw `preupd/postupd-<c>-<stage>.stdout` and `snapshot-*` JSON snapshots,
inspect templates, ps-leftovers, daemon-info, and the frozen report's
commands/hashes/params/final_state (reusing the v4 production
`reanalyze_shares_run.replay`). Nothing is measured or invented; immutable
inputs and the normalized original report are untouched. It asserts and
writes, per run:

```
frozen_defect_probe_streams : fail          (stored mutated evidence)
rehydrated_matches_original_17 : true
evaluate_twice_identical : true
input_byte_identical : true
frozen_verdict / rehydrated_verdict : indeterminate
```

Products: `reanalysis/v6/v6-rehydration.json` (`1ed0485a…`). The original
v4 reanalysis still reproduces its identical 17 rows after the purity change.

## 4. Read-only stall vs workload-progress analysis

`scripts/analyze_shares_stall.py` (`36bbdb77…`),
`reanalysis/v6/v6-stall-analysis.json` (`9388b4c8…`). Using only recorded
counters across emission gaps and long captures:

* **9 progressing gaps, 0 quiet gaps**: cgroup `usage_usec` and
  `CLOCK_PROCESS_CPUTIME_ID` advanced at substantial rates (~0.1–0.63 of a
  core) while no samples were emitted.
* The quoted **A 11.490377984 s capture** (t=35.480→46.970, weight 59/59
  stable, no quota — `max_stable=1`, `steal_ns=0`): across the span to the
  neighbor samples the process accrued ~**4.92–5.1 CPU-seconds over 12.16 s
  ≈ 0.405 core**, and sampler-thread CPU is negligible (2% over the run).

Interpretation: the busy workload **kept progressing**; the
**observation path stalled** (sampler descheduled/blocked on a bracketed
syscall or cgroup read, without quota throttling) — not actual workload
inactivity. The read instant of cpu.stat inside that one long capture is not
pinnable from current data (only surrounding counter deltas), so behavior
within the span is unobservable and the checker correctly quarantines it;
the run cannot be scored through the gap. This is a read-only diagnostic, no
verdict and no threshold change; finer per-file read timestamps belong to the
round10/CPU-witness capture-time-guard work and are left to that agent (no
kernel work duplicated here).

## 5. Tests (`tests/test_cpu_share_checks.py`, `a37ce0bc…`)

New `EvaluatorPurityTests` (synthetic, always run): brackets not normalized
in place, no `stage_snapshots` injected, evaluate-twice byte-identical, and
an explicitly normalized bracket still fails (purity does not mask bad
evidence). New `ActualV6ReplayPurityTests` (skip unless the immutable v6
dir is present; it is): frozen direct replay = historical defect;
rehydrated raw = exact original 17 and indeterminate; helper end-to-end;
stall analysis sees ≥1 progressing gap, zero quiet gaps, and the 11.49 s
capture progressing below 1 core.

Matrix: **113 tests OK** (7 new) including 31 seed checks; real-binary
classes remain green with `HARDDIND_SHARE_PROBE`. `scripts/*.py` compile;
no probe change (hash identical to v4); seed `src/cpu_limit_probe.c`,
owner/round10/round-v5/v6 immutable dirs, and `/root/udock` untouched.

## 6. Files

| File | sha256 |
|---|---|
| scripts/cpu_share_checks.py | `4eed8d6709b961a8102af55c4429e64ef93b0133709864b5b140358e1cd42acd` |
| scripts/rehydrate_shares_v6.py (new) | `ce5c26a16f265c4d3f11875c080a3b42280f3f8d98f70d16ec065c110774bdcd` |
| scripts/analyze_shares_stall.py (new) | `36bbdb775a28deb613ab9597198aecbe30d780f52bb517ec5e9cfec008541046` |
| tests/test_cpu_share_checks.py | `a37ce0bc5ecf8efe9b9138e07ed13348595b912f5cf6ca1478d1276e5700816f` |
| reanalysis/v6/v6-rehydration.json | `1ed0485a29b5b1e35ceeaa7ff6e09f68d1d1dbb042c2621cbfd63be638efa899` |
| reanalysis/v6/v6-stall-analysis.json | `9388b4c8e749c15c685f8775959de7f94e509be152c6d7ab2336a113c0e05055` |

Probe `3ecabf9e…` and runner `32013f9e…` unchanged from v4/v5.
