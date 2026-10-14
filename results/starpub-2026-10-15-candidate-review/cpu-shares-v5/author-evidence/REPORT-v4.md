# Docker `--cpu-shares` experiment — v4 report (graceful completion + replay)

Date: 2026-10-14 (UTC). Supersedes v3 (`REPORT-v3.md`). Runtime remains
owner-scheduled; **no runtime PASS is claimed** here. Status: code and
host-only validation complete.

## 1. Owner findings from the actual v3/v4 run

Immutable run: `/root/harddind-rebuild/cpu-shares-owner-v4/run/results/shares-v4/`.

1. **Probe brace bug (FAIL).** `json_share_cg` ended with `fputs("}}")`
   after `cpu.stat` was already closed, so real contend START/END lines had
   one extra `}`; the 58 v3 tests missed it because `capture-test` exercises
   only the sample emitter. The owner fixed that one line and added a real
   100 ms `contend` endpoint regression (old binary invalid first/last JSON;
   fixed binary all valid; 59 tests). Both are **preserved**: my probe starts
   from the owner's fixed file (`06773165…`), and the repo contains the
   owner's `RealContendStream` test.
2. **Actual run verdict INDETERMINATE (correctly, honestly).** Raw logs show:
   phase 0 (weights 100/59) fraction **0.62865..0.62895** vs expected
   **0.62893** — matches; the reversed phase had B sampler stalls of
   **0.76 / 1.89 / 0.70 / 0.57 / 0.44 / 0.38 s** (coverage gaps > 0.5 s);
   the equal-weight phase had only a **3.311 s** valid joint window
   (< 4 s; replay-confirmed below). Everything else passed, inputs stable,
   normal stop, clean groups. These are not relabeled normal and no
   threshold/gap was widened: both phases stay INDETERMINATE.
3. **`report['evidence']` was always `{}`** — `evaluate(ev)` was called on a
   local dict and discarded. Fixed: the complete evaluated evidence and
   per-command raw-parse replay records are now serialized.
4. **Scheduling contract.** Sequential startup/stage-snapshot/update command
   overhead ate the final phase (the v4 anchors were host-time; B's last
   update landed with only ~3.3 s left before A's fixed deadline). Fixed with
   bounded explicit graceful completion (§2.3), without widening the
   functional band or `MAX_GAP_S=0.5 s`.

## 2. Changes

### 2.1 Probe — `src/cpu_share_probe.c` (`3ecabf9e…`, 1115 lines)

Owner-fixed v4 capture file plus additive graceful-completion support
(v4 modes otherwise unchanged; verified build-clean under global EX lock):

* SIGTERM/SIGINT install an async-signal-safe `stop_requested` flag. The
  sampler sleeps in 10 ms chunks so a stop is noticed within ~10 ms; workers
  check the same flag in their spin loop. A stop never starts a new capture;
  a capture already in flight is completed (it stays bracketed) and emitted
  with `"terminal":1,"graceful":1` if it straddles shutdown — the checker
  excludes that span from rates.
* END gains `"graceful_stop","stop_ns","hard_deadline_ns"`: the checker
  requires `start ≤ stop_ns ≤ cap_end`, `stop_ns → flushed end ≤ 2 s`, and
  `hard_deadline_ns == start.load_deadline_ns`. Deadline-driven exit (no
  signal) reports `graceful_stop:0` exactly as before.
* Hard operational ceiling unchanged (`contend --duration-ms`, ≤ 120 s).

### 2.2 Checker — `scripts/cpu_share_checks.py` (`1c8073d9…`, 1454 lines)

* Schema-aware command ledger: v4 requires the new `stop-a`/`stop-b` labels
  (42 mandatory); schema-3 frozen runs replay without them. Unknown,
  duplicate or malformed rows still fail closed.
* `capture_protocol` verifies completion integrity: both containers must end
  in the same mode; a schema-4 run on the hard ceiling instead of graceful
  completion FAILs; mixed modes FAIL; stop→end latency and `stop_ns`
  ordering are bounded; terminal captures are quarantined and never anchor a
  rate.
* All functional constants unchanged (`MIN_WINDOW_S=4`, `MAX_GAP_S=0.5`,
  bands ±0.09/±0.08, saturation [0.85, 1.15], etc.). Slow stable captures
  anchor intervals with the wide/narrow plausible-duration box; gaps stay
  gaps; a missing required window stays INDETERMINATE/FAIL.
* The runner bug of empty evidence is outside the checker but is now
  observable: reports self-assert `evidence.containers` and
  `evidence.raw_replay` non-empty after write.

### 2.3 Runner — `scripts/run-cpu-shares.py` (`c3ed7523…`, 582 lines)

* schema_version 4. New explicit bounded completion: at each stage all
  pre-bracket snapshots are taken first, then all `docker update`s, then all
  post-brackets (sequential exec latency no longer lands on later
  containers); stage snapshots follow. Anchors are earliest-start points and
  are never piled. After the final stage confirms the observed equal weights
  on both containers (`A == B`, B moved off its middle weight — observations
  only, no shares math), the runner holds the busy workload for a fixed
  **`--stable-window-ms` (default 4500, floor 4000)**, then sends
  `docker kill --signal=TERM` to both containers in one instant (`stop-a`,
  `stop-b`), bounded-waits exit 0, collects logs/finals.
* Each probe's hard ceiling = head + 3·phase + stable window + a **declared
  `--overhead-budget-ms` (default 15000)**; ≤ 120 s. If real command overhead
  exceeds the budget and the ceiling fires first, the missing final window
  fails/indeterminates — overhead can never silently consume a phase.
* `report['evidence']` now holds the complete evaluated evidence (JSON-safe)
  and `evidence.raw_replay` indexes every recorded command output with path,
  byte size, parsed event counts and any JSON parse errors.

### 2.4 Reanalysis — `scripts/reanalyze_shares_run.py` (`b1d93517…`)

Rebuilds evaluator evidence purely from an immutable results directory
(logs, bracket/snapshot/inspect stdout, ps-leftovers, daemon-info, and the
frozen report's commands/hashes/params/final_state — no fabrication; missing
artifacts fail), runs the current checker, and compares against the frozen
17 statuses. It writes a separate file; the original snapshot/report is never
modified.

## 3. Replay of the immutable v4 run (separate file)

`reanalysis/shares-v4-reanalysis.json` (`070bc771…`), produced from
`/root/harddind-rebuild/cpu-shares-owner-v4/run/results/shares-v4`:

```
frozen_verdict   : indeterminate
replayed_verdict : indeterminate
identical_17     : true        status_diffs: {}
```

21 raw-replay records, logs-a 346 / logs-b 328 events with zero parse
errors. Replay detail matches the owner exactly: phase 2 joint wide window
3.311 s / 3.307 s (< 4 s → too short); reversed phase coverage gaps 0.76,
1.89, 0.70, 0.57 s (the 0.44/0.38 fall under the 0.5 s split threshold).
`weight_mapping` replay recorded observed **100/59** and computed
1024→100 / 512→59 (pinned log2-fit primary source), with the computed values
still labeled as pending-observation outside an actual run.

## 4. Validation (host-only, bounded ≤ 60 s, global EX, ≤4 jobs)

* `tests/test_cpu_share_checks.py`: **69 tests, OK**, including the owner's
  real 100 ms endpoint regression, a real bounded SIGTERM graceful-run test
  (exit 0, `graceful_stop=1`, ordered `stop_ns`, bounded flush), FIFO
  flip/long/malformed/absent/resume capture fixtures, graceful-vs-deadline
  and mixed-mode failure, terminal-capture exclusion, stop_ns ordering and
  2 s flush bound, schema-3 legacy ledger vs schema-4 stop-label
  requirement, and all prior bracket/deletion/non-overlap/stall cases.
* Real-binary classes run with
  `HARDDIND_SHARE_PROBE=/tmp/cpushare-v4/probe`: 7/7 OK. Probe rebuilt under
  `flock -x kernel-build.lock gcc -O2 -Wall -Wextra -Werror -static
  -pthread`, selftest all parsers pass.
* Seed regression `test_cpu_limit_checks.py`: 31 OK. Combined discovery:
  **100 tests OK**. Runner failure path (fake gcc) persists a fail-closed
  schema-4 report whose `evidence` is now fully populated; argparse rejects
  stable windows < 4 s and out-of-range budgets before any side effect.
* No guest/VM/GPU/busy benchmark by this agent (only 100–500 ms bounded
  fixture workloads for parser/SIGTERM validation); immutable owner/round10
  files and the unrelated `src/cpu_limit_probe.c` untouched; `/root/udock`
  untouched.

## 5. Owner rerun

```
python3 scripts/run-cpu-shares.py <WORK> --kernel <k> --disk <d> --name <n> \
  [--phase-ms 9000 --stable-window-ms 4500 --overhead-budget-ms 15000]
```

schema_version-4 `report.json` with full evidence + raw replay; exit
0 pass / 1 fail / 2 indeterminate-or-not_tested. Defaults give ~51.5 s hard
ceiling per probe and a guaranteed ≥ 4.5 s observed final stable window; if
guest stalls remove required coverage the verdict remains honest
INDETERMINATE (as the v4 run did), never a concealed pass.
