# Docker `--cpu-shares` contention experiment — v3 report (bracketed captures)

Date: 2026-10-14 (UTC)
Status: **v3 code complete, statically and synthetically validated against the
real probe binary; runtime remains owner-scheduled. No runtime PASS is
claimed.** Supersedes v2 (`REPORT-v2.md`, frozen in
`/root/harddind-rebuild/cpu-shares-owner-review-v2`); the rejected v1 report is
also retained.

## 1. Why v2 was still wrong

Two defects remained after the v2 missing-evidence repair:

1. **Single-timestamp "same instant" claim was false.** The frozen v4
   evidence shows an unquotaed cpuset-0 read capture taking **4.718839808 s**
   (`/root/harddind-rebuild/cpu-limits-owner-v4/run/results/cpu-limits-v4/logs-cpulim-cpuset0.stdout`,
   7 samples, `cap_dur_ns` max 4 718 839 808). `read_cg()` is multiple
   syscalls (cpu.max, cpuset.cpus, cpuset.cpus.effective, cpu.weight,
   cpu.stat); a single "before" timestamp can stamp a counter read seconds
   earlier. My v2 continuous weight timeline attached one time to a weight
   and a stat read that could be seconds apart, and ended phases at point
   timestamps — so an update landing during a slow/descheduled capture could
   still be misattributed.
2. **Wrong shares→weight formula and an over-claimed observation.** v2 quoted
   `(shares-2)*9999/26214` (denominator wrong even for the legacy formula) and
   called 39/20 "observed on this stack" though no runtime had run.

## 2. The frozen v4 capture contract (copied, not merged)

`src/cpu_share_probe.c` (sha256
`c3e33429904e83c6ad32d97e6e60d6a6194f68f15e1f1638f180caa54808b4a7`) was
created by copying the **frozen, owner-authorized** v4 probe
`/root/harddind-rebuild/cpu-sampler-owner-review-v4/src/cpu_limit_probe.c`
(sha256 `3711d1352a52dac4c1f64eed46aef3a0df9d9bdbe14d664834268b2e13944efce`).
No round10 or owner file was edited. Brace-accurate comparison proves the v4
modes are unchanged: `cmd_spin`, `capture`, `emit_cap_sample`, `read_cg`,
`read_raw`, persistent-fd helpers, `parse_stat`, `parse_cpu_max_v`, steal
diagnostics, `cmd_info`, `cmd_snapshot` are byte-identical. Only additive
extensions exist:

* `held_read`/`held_read_line`: an ESPIPE branch lets the **fixture harness
  only** stream one message from a FIFO (regular cgroup path unchanged).
* `cmd_selftest`: additive `parse_weight` subcheck (output now includes
  `"parse_weight":"pass"`).
* `cmd_capture-test`: optional `--weight P --cpuset P` switches it onto the
  share capture so the real capture code is testable against files/FIFOs.
* New, separate functions `parse_weight_v`, `capture_shares`,
  `emit_share_sample`, `json_share_cg`, and a new **`contend`** mode
  dispatched from `main`. Existing v4 schemas are untouched.

### contend capture order (read failures are first-errno, counted, never retried)

```
wall_ns ─► cpu.max(1) ─ cpu.weight(1) ─ cpuset.effective(1)
           ─► cpu.stat
           ─► cpu.weight(2) ─ cpuset.effective(2) ─ cpu.max(2)
           ─► steal ─ CLOCK_PROCESS_CPUTIME_ID ─► cap_end_ns
```

Every sample emits: `wall_ns`, `cap_end_ns`, `cap_dur_ns == cap_end-wall`,
`proc_cpu_ns`, `sampler_cpu_ns`, `weight1/2`, `weight1/2_errno`,
`weight1/2_invalid`, `weight_stable`, `cpuset1/2_errno`, `cpuset1/2_raw`,
`cpuset_stable`, both `cpu.max` pairs with `max_stable`, `share_valid`,
`stat_errno` and all cpu.stat counters. Start/end events carry the same
bracket (`cpu.weight2`, `cpuset.cpus.effective2`, stability markers,
`cap_end_ns`), plus workers, `worker_cpu_ns` and `sampler_cpu_ns`
(workers+sampler thread accounting). Strict rules: no within-capture retry,
the first errno is preserved, parse failures are emitted as `*_invalid` with
EPROTO errno and counted; a failed persistent fd is reopened only for the
*next* capture.

Build evidence: the current source compiles clean under
`flock -x /root/harddind-rebuild/kernel-build.lock gcc -O2 -Wall -Wextra
-Werror -static -pthread` (single bounded gcc; lock currently held by owner
activity, so a redundant second build was not queued); `selftest` reports all
parsers pass. No `contend`/busy spin, guest, GPU or native load was run here;
only `selftest/info/snapshot/capture-test` were executed on the host
(capture-test reads fixture files — zero CPU load).

## 3. Checker: quarantine + plausible-duration boxes (no false PASS / false FAIL)

`scripts/cpu_share_checks.py` (sha256
`9ac93d62f29c22c2723becfb2600440180107eb36dc32e45a7b365f3fa32e7ef`).

**Sample validation (schema is fatal).** Missing required fields, reversed
capture clocks, inconsistent `cap_dur_ns`, non-monotonic counters, quotas,
escaped cpusets, non-`contend` streams → FAIL. Read errors on the *invariant*
files (`cpu.max`, `cpu.stat`, cpuset) at any periodic sample → structural
FAIL; `cpu.weight` read/parse errors (the file under experiment) are
preserved error captures, counted and quarantined.

**Timeline reconstruction.** Stable-fast captures form chains; runs are
indexed by epoch (A:[high0, low1], B:[low0, high1, low2]) so B's two light
epochs are never merged. Splits occur on: unstable brackets
(weight1≠weight2), error captures, slow captures (`cap_dur >
CAPTURE_FAST_S=0.05 s`), and coverage gaps > 0.5 s. An unstable capture is
accepted **only** at a planned transition, with nearest stable captures on
both sides differing and bracket reads matching old→new in capture order; a
flip-and-revert or a backward read FAILs the timeline. Slow **stable**
captures are not dropped: they anchor adjacent chains, and their full spans
enter the rate math.

**Joint windows with uncertainty intervals.** For two endpoint captures
(a,b), CPU delta elapsed over some w ∈ [`b.wall_ns-a.cap_end_ns` (narrow),
`b.cap_end_ns-a.wall_ns` (wide)], so each per-container rate is an interval
[delta/wide, delta/narrow]. Joint fraction extrema over both containers'
boxes: `f_lo = rA_lo/(rA_lo+rB_hi)`, `f_hi = rA_hi/(rA_hi+rB_lo)`, computed
for both proc and stat clocks.

Three-valued decision per interval (matching the owner's v4 counterexample
`owner-uncertainty-counterexample.json`, where the source-copy v4 checker
false-FAILED a straddling bound):

* **PASS** only if the *entire* plausible box lies inside the functional
  band and satisfies every invariant;
* **FAIL** only if *every* plausible value violates (fraction fully outside
  the observed-weight band; smallest plausible total > 1.15 core; largest
  plausible allocation still starved);
* otherwise (straddles a valid/invalid boundary) **INDETERMINATE**.

Additional guards: proc/stat plausibility intervals must come within 0.05
cores; fraction intervals within 0.05; saturation band [0.85, 1.15] over the
whole box; a read-error/unstable/coverage gap inside a phase's run epoch
makes that phase INDETERMINATE even if a clean remnant exists (measured
coverage must also be ≥ `MIN_PHASE_COVERAGE=0.7`); ≥ 4.0 s and ≥ 20 captures
per interval; endpoint skew ≤ 0.5 s. Functional tolerances are the unchanged
v1 constants (±0.09 weighted, ±0.08 equal, ordering margin 0.05) — there is
no throughput/performance threshold.

**Fail-closed v2 machinery retained:** checker-owned 40 mandatory command
labels (unknown/duplicate/malformed rows FAIL; `poll-*` prefix the only
repeatable and still audited), canonical update plan, 8 mandatory input hash
roles with non-empty 64-hex before/after digests, present-string leftovers
inventory, strict stage snapshot fields, missing check rows and runner error
lists force FAIL; fail/indeterminate/not_tested remain distinct.

## 4. Shares→weight: pinned formula, labeled computed pending runtime

The pinned daemon source is
`moby-port-pristine/vendor/github.com/containerd/cgroups/v3/cgroup2/utils.go`
(and the identical `opencontainers/cgroups/utils.go`); `ToResources` calls
`ConvertCPUSharesToCgroupV2Value`, a **log2 quadratic fit**:

```
l = log2(shares); exponent = (l*l + 125*l)/612 - 7/34; weight = ceil(10**exponent)
```

Computed labels from the pinned primary source: **1024→100, 512→59**
(bounds 2→1, 262144→10000). These are recorded in `weight_mapping` evidence
as `computed_weights_pending_runtime` with the source citation; they are
*not* treated as observed. Verdict fractions always derive from the weights
actually read inside the containers (`whi/(whi+wlo)`), and the observed
mapping must be internally consistent, monotonic, equal across stages, and
consistent with stable start/end brackets. Whether the guest really shows
100/59 is unknown until the owner runs it; the report labels it so.

## 5. Files

| File | sha256 | Lines |
|---|---|---|
| `src/cpu_share_probe.c` (v3; copy of frozen v4 + additive contend) | `c3e33429904e83c6ad32d97e6e60d6a6194f68f15e1f1638f180caa54808b4a7` | 1073 |
| `scripts/cpu_share_checks.py` (v3) | `9ac93d62f29c22c2723becfb2600440180107eb36dc32e45a7b365f3fa32e7ef` | 1386 |
| `scripts/run-cpu-shares.py` (v3, schema_version 3) | `981e3eb52c219ec47b9305f5d9daca4b5bdf6d32a1d2e59ef67312af0f4cd3a9` | 460 |
| `tests/test_cpu_share_checks.py` (v3, 58 tests) | `a5f0b779a6e71fdf263e2444c40ee1a72b63a534d316cb22e659c51fa0664895` | 878 |

Seed/common files verified unmodified (mtimes at/before 19:30):
`src/cpu_limit_probe.c` (f383, `f6791d6b…`), `scripts/harddind.py`,
`scripts/guest_cleanup.py`, `scripts/run-cpu-limits.py`,
`scripts/cpu_limit_checks.py`, `tests/test_cpu_limit_checks.py`; nothing
under round10 or either frozen owner directory was touched. `/root/udock`
untouched. (Own hash of this report omitted by necessity.)

## 6. Validation performed (bounded ≤ 60 s, single test process, no busy load)

Environment: Python 3.10.12, gcc 11.4.0, Linux 5.15 host.

1. **Synthetic checker tests** `timeout 60 python3 tests/test_cpu_share_checks.py`
   → **58 tests, OK**. Highlights: baseline pass with fractions from
   observed 100/59 boxes; the real owner scenario — a 4.719 s capture
   straddling the A weight change — is fully quarantined, both phases still
   score on clean interiors (PASS), and no scored interval intersects the
   straddle; a 5.5 s delayed B straddle leaves its heavy phase too short →
   INDETERMINATE; a 5 s stable capture mid-phase → INDETERMINATE with
   evidence retained; two 5 s stable endpoint captures in both containers →
   INDETERMINATE through the plausible box; exact three-valued unit cases
   (straddle→indeterminate, fully outside→fail, contained→pass); weight
   flip/malformed/absent/resume; extra flips and backward bracket reads FAIL;
   coverage gaps never pass; all v1/v2 deletion attacks fail closed; ledgers,
   roles, identity, topology, lifecycle; pinned formula values 100/59.
2. **Real binary capture fixtures** (env
   `HARDDIND_SHARE_PROBE=/tmp/cpushare-v3/probe`): the actual
   `capture-test` binary is driven against regular files and FIFOs —
   stable reads (share_valid 1), malformed weight (errno 71, invalid 1),
   absent→resume across captures (errno 2 then 0), a FIFO feeder delivering
   `100` then `59` 1 s apart within one capture (weight_stable 0,
   `cap_dur_ns` > 0.9 s — the delayed-read-straddling-change case), and a
   1 s stable capture (valid, honestly long). **58 tests OK including all 5
   binary tests.**
3. **Adversarial sweep** against v3: delete commands/inputs/leftovers, empty
   ledgers/inputs/streams/state, deleted snapshot/sample bracket fields,
   max/stat/cpuset errnos, `cap_end`/`cap_dur` tampering, quota, legacy spin
   mode, mid-phase weight ENOENT — none can pass; baseline still passes.
4. **Seed regression**: `tests/test_cpu_limit_checks.py` → **31 tests, OK**;
   combined discovery `python3 -m unittest discover -s tests -p
   'test_cpu_*.py'` → **89 tests, OK** (5 env-gated binary fixtures show as
   skipped without `HARDDIND_SHARE_PROBE`).
5. **Runner**: label set matches the checker-owned mandatory labels exactly;
   failure path with a fake failing gcc exits 1 and persists a fail-closed
   schema_version-3 report (ledger/inputs/leftovers/lifecycle fail, phases
   not_tested, raw stderr retained); argparse rejects bad names, non-4
   guests, bad cpu index and timing before any side effect.

## 7. Owner execution (unchanged interface; runtime not run by this agent)

```
python3 scripts/run-cpu-shares.py <WORK> \
  --kernel <kernel> --disk <runtime.ext4> --name <immutable-name> \
  [--cpus 4] [--shared-cpu 2] [--threads 4] [--phase-ms 9000] [--sample-ms 100]
```

Output: `<WORK>/results/<name>/report.json` (schema_version 3) with 17 named
check rows, per-command stdout/stderr, pre/post bracket snapshots, both full
bracketed contend JSONL streams (every capture span and errno preserved),
quarantine intervals, per-phase fraction/rate boxes, coverage fractions, slow
anchors, observed-vs-computed weight mapping and input role hashes. Exit
0 pass / 1 fail / 2 indeterminate-or-not_tested. Bounded guest busy spin
~32 s at defaults; schedule under the owner queue and the global
kernel-build.lock protocol.

## 8. Pending runtime boundary

Not observed by any run in this delegation: whether the guest daemon writes
cpu.weight 100/59 (pinned formula, computed label only) or something else;
real `docker update` latency and straddle frequency; whether phase interiors
clear 4 s at default 9 s spacing (rerun `--phase-ms` up to 20000 on
INDETERMINATE); actual allocation fractions; clean lifecycle. The v4
4.719 s capture proves multi-second bracketing is operationally necessary,
not that shares enforcement passes. After round10's v5 checker merges, the
contend schema can adopt its finalized stable/error capture accounting; the
weight-timeline and plausible-box logic here is independent of that path. No
runtime acceptance or performance claim is made.
