# REPORT-v5 — capture-uncertainty false-FAIL correction

Status: **checker code complete; no runtime claim. No VM/guest/GPU/native load
was started by this agent; the owner's v4 guest run is the only actual CPU
evidence and remains `not_tested`→owner-verdicted by the owner.** This v5 is a
checker/test repair against the owner's frozen synthetic counterexample
(`/root/harddind-rebuild/cpu-sampler-owner-review-v4/owner-uncertainty-
counterexample.json`), plus the required attribution correction to REPORT.md.
The v4 probe showed no new defect and is untouched; the runner is untouched;
no CFS accounting constant changed and no failure was turned into a pass.

## 1. Changed files and hashes

| File | v4 frozen | v5 |
|---|---|---|
| `src/cpu_limit_probe.c` | `3711d1352a52…4efce` | **unchanged** `3711d1352a52…4efce` |
| `scripts/run-cpu-limits.py` | `d88dc06101…64c59` | **unchanged** `d88dc06101…64c59` |
| `scripts/cpu_limit_checks.py` | `04a9824e8b…a5b7f` | `96ea657d47…c276` |
| `tests/test_cpu_limit_checks.py` | `a2d8d59a4f…7d417` | `26e412c34d…a4939` |

Full v5 hashes:

```
3711d1352a52dac4c1f64eed46aef3a0df9dbdbe14d664834268b2e13944efce  src/cpu_limit_probe.c     (unchanged)
d88dc06101e5317ba6351d698cf05528e16a42cd746093b4739de31fbd964c59  scripts/run-cpu-limits.py (unchanged)
96ea657d47c71649c82dd3616e24d820aab1d2c41f7a58b7cf26d6e2624bc276  scripts/cpu_limit_checks.py
26e412c34dd5aa32c33d5d015cd5725b9de7076de6dedc5764f0e674ea6a4939  tests/test_cpu_limit_checks.py
```

(REPORT-v5.md itself is delivered alongside; v4 REPORT.md is left as the
historical artifact — see §5.)

## 2. The defect (owner counterexample, confirmed)

`_window_result` / `classify_quota` declared FAIL when the **highest**
plausible rate over the **shortest** plausible counter interval exceeded the
bound at that shortest interval. That ignores the longer intervals the
brackets also admit, where the same delta can be legal:

owner-frozen numbers — quota 0.5 cores / 100 ms (K = 2·c·p + sliver =
0.125 s·cores); a=(wall 0, cap_end 20 ms, counters 0),
b=(wall 1.020 s, cap_end 1.040 s, Δ 0.635 s), throttle 8 / 500 ms:

- narrow interval 1.000 s → rate 0.635 > U(1.000) = 0.625 (v4 → FAIL)
- wide interval 1.040 s → rate 0.610577 < U(1.040) = 0.620192, i.e. a legal
  schedule exists ⇒ violation is **not proven** ⇒ INDETERMINATE required.

The error could only invent a FAIL (conservative direction — the owner ran v4
raw anyway), never a false PASS, but a false FAIL is still wrong.

## 3. Exact v5 logic (three-way proof)

For counter delta D over an endpoint pair, the true elapsed interval lies in
`[narrow, wide]`; the CFS upper bound `U(w) = c + K/w` is decreasing in w:

- **FAIL — violation proven**: least plausible rate `D/wide` **> U(wide)**
  (every interval consistent with the brackets violates).
- **PASS-side upper proof complete**: worst plausible rate `D/narrow`
  **≤ U(narrow)** (no admissible interval violates).
- **between**: INDETERMINATE — "capture interval crosses the quota upper
  bound: violation neither proven nor excluded by the evidence". Never FAIL,
  never PASS.

Independent counter handling preserved:

- update windows use process clock and `cpu.stat` usage (both required);
- fixed phases additionally include the independent workers+sampler clock in
  the upper proof (`least`/`worst` across all three);
- the **lower delivery** bound uses only process/stat — worker clocks
  legitimately exclude other in-cgroup work (the v1 fixture: workers 0.477
  vs cgroup 0.493 must stay PASS). During this fix that gate briefly used the
  worker clock too and was caught by the preserved
  `test_sampler_cpu_is_part_of_cgroup_quota` test;
- ordering otherwise unchanged: quota-setting mismatch FAIL → proven upper
  violation FAIL → missing throttle counters INDET → counter disagreement
  INDET → upper-crossing INDET → below-delivery INDET → reference-separation
  INDET → PASS. Throttling evidence, counter agreement, reference separation
  and the unchanged CFS constants (`SLIVER_S=0.025`, `EXTRA_PERIODS=2`,
  guards 0.5/0.2 s, `CAPTURE_MAX_S=0.025`, `MIN_INTERIOR_S=1.0`) are intact.
- evidence gains `upper_wide`, `lower_narrow`, `least_rate`, `worst_rate`;
  all prior fields are retained.

## 4. Bounded test results (all ≤60 s, parser/model only)

Owner guest SH on `kernel-build.lock` was active during this work; no EX build
was taken. Test binaries reused the frozen v4 probe
(`…/cpu-limits-owner-v4/run/results/cpu-limits-v4/cpu-limit-probe`,
`8c5ce0b8…97740`, source `3711d135…`) via a new `CPU_LIMIT_PROBE_BIN`
environment override in `CaptureExecutableTests` (gcc fallback retained and
already v4-validated; one ~0.7 s userspace fallback compile of the unchanged
source occurred during a path-override check — no kernel/guest/host change).

```
CPU_LIMIT_PROBE_BIN=…/cpu-limits-v4/cpu-limit-probe \
  timeout 60 python3 -m unittest tests.test_cpu_limit_checks
  → Ran 68 tests … OK
CPU_LIMIT_PROBE_BIN=…/cpu-limits-v4/cpu-limit-probe \
  timeout 60 python3 -m unittest discover -s tests
  → Ran 107 tests … OK
```

(v4 had 60 / 99; v5 adds 8 checker tests, all 60 v4 CPU tests preserved and
passing.) New tests:

- `OwnerUncertaintyCounterexampleTests` — the owner's verbatim a/b geometry:
  crossing 0.635 → **INDETERMINATE** with exact rate/bound equalities;
  0.70 → FAIL, 0.60 → PASS on the same endpoints.
- `FixedCaptureBoundaryTests` — fixed `classify_quota` crossing (0.635 →
  INDETERMINATE), definite violation (0.70 → FAIL), definite compliance
  (0.60 → PASS, reference supplied), and zero-width captures collapsing to
  the nominal band.
- Sparse update-window suite reworked: the v4 test that expected FAIL for the
  crossing case now asserts INDETERMINATE (`test_capture_uncertainty_crossing_
  is_indeterminate`), with new definite-violation and definite-compliance
  siblings on identical sparse v3-shaped timelines.

Preserved genuine negatives still enforced (spot list): below-bound sparse
starvation → INDET; 50 ms slow capture excluded → no valid window; in-capture
setting flips quarantined/fail; reversed clocks/counters/steal → FAIL;
missing cpu.max/stat/capture fields/sampler clock → fail/raise; steal absence
stays verdict-neutral.

Read-only confirmation reparse of the **frozen, unmodified v4 raw logs** with
the v5 checker (the owner independently reparses; this is not a rewrite of
their `report.json`):

- six fixed phases: all **pass**, identical to the frozen v4 verdicts;
- `update_window_initial_2`: **pass** (least 2.012 ≤ U(wide) 2.114);
- `update_window_half`: **indeterminate — below quota delivery bound**
  (unchanged verdict): 5.84 s stable interior spanning the excluded slow
  capture, proc/stat 0.4400 < lower_wide 0.4786, 59 throttling events /
  18.91 s throttled; slow sample index 51 preserved in `slow_indexes`;
- `update_window_recovered_2`: **indeterminate — window too short**
  (unchanged);
- `update_timeline`: **pass**.

The owner's false FAIL was therefore latent (not triggered by the actual v4
run); the v5 change changes no v4 verdict and makes the honest three-way
classification available for the owner's independent reparse.

## 5. Correction to v4 REPORT.md §2 (recorded observations vs inference)

The v4 REPORT's root-cause section correctly recorded the v3 observations but
overstated one inference. Corrected statement:

- **Recorded fact (internal inconsistency):** unbracketed v3 timestamps
  cannot date the counter reads. The stamped-9.175 interval showing
  5.4 cores / 110 CFS periods in 0.20 s and the 11.775 sample already
  containing the 200000 quota (write landed ≈12.2) prove only that reads
  completed materially after their stamps.
- **What the multi-second 0.087–0.145-core gaps cannot establish:** genuine
  host/vCPU non-placement, quota-deferred sampler execution, guest-internal
  scheduling, and stamp/read skew are **not distinguishable** from the
  unbracketed v3 samples. Outer-host loadavg (~36→40) and pressure are
  environment context, not causal proof. No claim of host starvation and no
  kernel-bug claim follows.
- **What bracketed v4 adds (observed, still not a cause):** one capture
  starting 9.639 s ended 3.402 s later with 36 periods (≈3.6 s of CFS wall)
  read inside the bracket — userspace did not complete its reads for 3.4 s;
  the checker preserves and excludes that capture. The subsequent stable
  interior averages 0.440 cores over 5.84 s. These bracket facts are
  compatible with host descheduling of the UML guest process and with
  guest-internal non-placement; the bracket bounds the stall, it does not
  name the layer.
- **Steal-clock limitation:** v4 guest `/proc/stat` steal was **0 for the
  whole run**, including across the 3.4 s stalled capture. The diagnostic is
  whatever the guest kernel itself accounts; in a UML guest it evidently may
  not record host-process descheduling, and v5 makes no claim that it detects
  host starvation. It remains a strictly optional corroborating channel whose
  absence is verdict-neutral.

Distinguishing the hypotheses requires the owner's bracketed v4/v5 evidence
together with host-side scheduling data (out of this agent's read-only scope);
it cannot be back-filled from v3.

## 6. Actual-v4 observables vs unsupported causality (OWNER-ACTUAL-V4-RESULT)

Read from the frozen, unmodified v4 raw logs. These are **observations from
existing probe fields**, not causal claims; no limit was loosened and the
cpuset0 phase verdict remains **pass**.

**cpuset0 (`logs-cpulim-cpuset0.stdout`, 6.185 s phase, deadline 6.0 s):**

- sample 5 capture started at 1.283 s and completed at 6.002 s:
  **`cap_dur_ns` = 4.719 s stall**, fully bounded by the v4 brackets (the
  other six captures were ≤21 ms);
- while that capture was open, the cgroup was charged **4.98 s of CPU**
  (`usage_usec` between adjacent completions 1.015 s and 6.002 s) =
  **0.999 cores for 4.987 s**, i.e. one core was continuously delivered;
- phase totals agree: workers 4.65 s (1.15–1.17 s each), sampler thread
  **1.34 s**, process/stat 5.99 s ≈ 0.97 cores average; all four workers'
  start/end affinity and `sched_getcpu` residency were exactly CPU0;
  cpuset-only cgroup shows zero quota periods/throttling (as expected);
- guest steal stayed 0 across the stall.

Separating observable from hypothesis: aggregate guest/host starvation ("the
guest got no CPU") is **contradicted as a complete explanation** by the cgroup
consuming ~one full core while the sampler's capture did not complete; the
observed stall is specifically of the sampling thread's read completion.
Whether that thread stalled in guest scheduling, the UML syscall/read path, or
host scheduling of its host task is **not distinguishable from in-guest
fields** (the sampler itself accruing 1.34 s thread-CPU during the phase is an
additional observable, not an explanation). No host-causality or kernel-bug
claim is made.

**update run (`logs-cpulim-update.stdout`):** the 3.402 s capture at
9.639→13.041 s contains 36 periods (~3.6 s CFS wall) and 1.48 s charged usage
between adjacent completions; quota transitions actually observed at ~8.5 s
and ~15.4 s (updates issued at harness 5/12 s). The stable half interior
(8 samples on each side of the excluded capture) averages 0.440 cores over
5.84 s → below the 0.5-core delivery lower bound → **indeterminate**, and the
recovered 2-core segment lacks the required stable interior →
**indeterminate**. Steal 0 throughout. These stay indeterminate by design.

## 7. Targeted next-probe recommendations (existing fields; owner-scheduled)

No new VM/load started by this agent; for the owner's optional stage-timing
diagnostic, all of these use the v4 schema already in the raw logs:

1. **Classify every stall with two existing numbers**: `cap_dur_ns` (wall
   stalled in the capture) and the `usage_usec` delta between bracketing
   adjacent samples divided by their `cap_end` delta (CPU charged while the
   capture was open). Long `cap_dur_ns` with ~full-rate usage (cpuset0)
   pinpoints a sampler-thread/read-path stall; long stall with ~zero usage
   would indicate aggregate non-placement. Both occurred in v3 speculation;
   only v4 brackets let them be told apart.
2. **Sampler-cost cross-check** already exists: start/end `sampler_cpu_ns`
   (1.34 s in cpuset0 vs 0.003–0.03 cores in unstalled fixed phases). A
   future owner diagnostic could emit the same thread-CPU clock once per
   capture to localize that burn — a probe-field addition for an owner-run
   diagnostic, not needed for the v5 verdict logic.
3. **Date stalls with counter periods**: inside-throttle runs, the
   `nr_periods` delta across a bracketed capture (36 in the update run) gives
   real CFS wall elapsed inside the stall independent of the stamped clock;
   cpuset runs lack periods, so use the `usage_usec` method of item 1.
4. **Steal is non-evidence in this UML guest**: it read 0 during a proven
   4.7 s in-capture stall; do not treat it as a host-deschedule detector.
5. If the owner wants the recovered 2-core window reachable after late
   effective transitions, lengthen the update spin in a future scheduled run
   (harness scheduling choice, e.g. `UPDATE_MS` 24 s) — do not shorten the
   0.5/0.2 s guards or `MIN_INTERIOR_S`.

## 8. Boundaries

No host cgroup writes, no `/root/udock`, no socket/KVM/VM/GPU/native
busy-spin/benchmark/resource change; frozen owner repos
(`cpu-limits-owner-v3`, `cpu-limits-owner-v4`, `cpu-sampler-owner-review-v4`)
were read only and are unmodified (hashes spot-checked); the v4 raw
`report.json` and logs are historical and were not rewritten.
`full_acceptance`/`performance_acceptance` remain unclaimed.
