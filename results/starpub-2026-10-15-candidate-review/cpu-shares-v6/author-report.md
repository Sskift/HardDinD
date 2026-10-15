# Docker `--cpu-shares` experiment — v5 report (finalization crash + metadata durability)

Date: 2026-10-14 (UTC). Supersedes v4 (`REPORT-v4.md`). Author ran no
VM/GPU; all validation host-only, bounded, global EX. The owner reruns.

## 1. Owner v5 failure (preserved, not relabeled)

Actual run `/root/harddind-rebuild/cpu-shares-owner-v5/` completed the full
contention sequence and shut the guest down normally, then **crashed during
report serialization**:

```
run-cpu-shares.py line 548
  'event_names': [e.get('event') for e in evs[:3]]
AttributeError: 'str' object has no attribute 'get'
```

Root cause: finalization indexed arbitrary command stdout line-by-line with
`json.loads` and called `.get` on every result. `instance-start.stdout` is a
**pretty-printed JSON object over 54 lines** (line 51 parses as a JSON
string), and `wait-a/b.stdout` is a bare JSON integer `0`. No `report.json`
was written → no functional verdict; by the owner's audit this is **FAIL**, and
structured PID/cleanup metadata was lost (only commands.log and raw outputs
survive). No full command-cleanup audit is claimed here; the audit
(`owner-postfailure-audit.json`) confirms a normal guest stop and four empty
groups, and explicitly marks the group audit unavailable.

## 2. Repair

### 2.1 Type-aware production indexer — `scripts/raw_output_index.py` (`0b613f67…`, new, 228 lines)

`index_results_dir()` classifies every archived output by label shape, never
calling `.get` on a scalar and never throwing:

* `logs-a/b` → strict **probe JSONL**: each non-empty line must be a JSON
  object; null/boolean/number/string/array lines and malformed lines are
  separate `parse_errors`; an empty/object-less probe file fails integrity.
* snapshot/pre/post/probe-selftest → whole-content JSON **object**
  (compact single-line or multi-line pretty print); scalar there fails.
* `instance-start/stop` → pretty whole-file JSON object.
* `wait-*` → **json_scalar** (number/string/bool/null), value recorded,
  never `.get`.
* everything else (inspect templates, daemon-info, kill output, gcc) → text;
  empty recorded as empty.

Diagnostic `errors` are reported separately; `integrity_ok` is fail-closed and
the runner turns it into an overall FAIL. Nothing is invented: a missing
commands.log or stdout is recorded, never reconstructed.

### 2.2 Runner — `scripts/run-cpu-shares.py` (`32013f9e…`)

* **Incremental durability before any later failure**: each command row is
  appended + fsynced to `command-metadata.jsonl` immediately (PID, exit,
  timeout, issued times, cleanup audit) before the next command; input hashes go
  to `input-hashes-before.json` before the guest starts and
  `input-hashes-after.json` at finalization. A crash can never again lose all
  metadata.
* Finalization uses the same production indexer (`raw-output-index.json` is
  written first), then the checker; the **complete evaluated evidence** is still
  serialized (v3 bug remains fixed), plus `raw_index_errors` /
  `raw_index_integrity_ok`.
* **Fail report guaranteed**: the serialization step is wrapped; if it raises, a
  stripped fail-closed `report.json` (`finalization_failure` reason) is written
  atomically. Verified live on a fake-gcc run: metadata JSONL, hashes and a
  `fail` report all persist.
* **Stop ordering corrected in prose/code**: SIGTERM is issued
  **sequentially, ordered stop-a then stop-b** (two `docker kill` calls),
  recorded in `schedule.completion` as
  `stop_issuance: sequential_ordered_a_then_b`, with `stop_order` and the
  measured issuance gap. No "same instant" claim remains.
* New production mode
  `--index-only <results-dir> --index-out <dir>` runs the **same indexer**
  over an immutable/crashed results dir without mutating it, writing
  `raw-output-index.json` and `index-report.json`.

Functional thresholds and graceful semantics are unchanged (schema 4;
4 s window, 0.5 s gap split, ±0.09/±0.08 bands, saturation
[0.85,1.15], bracketed captures, graceful-stop rules).

### 2.3 Read-only v5 recovery — no verdict

`reanalysis/v5/index-report.json` (`16b83f8c…`) and
`reanalysis/v5/raw-output-index.json` (`04f52f33…`) are produced by the
production `--index-only` path traversing the actual v5 archive:

* 46 labels indexed, integrity_ok **true** (real files archived cleanly;
  instance-start/stop pretty objects, wait-* integer 0, logs-a 190 / logs-b
  160 probe events, ps-leftovers empty — all classified), zero index
  errors;
* `report_present_in_source: false`, `complete_command_metadata: false`,
  `functional_verdict: null` — flagged incomplete;
* `raw_diagnostic_summary` (read-only arithmetic, explicitly **NOT a
  functional verdict**): this run had stalls up to **3.34 s** (A) and
  **3.29 s** (B), plus 0.5–1.1 s gaps; weight runs recovered
  (A 100@7.04–30.89 / 59@31.11–54.98, B likewise). No pass without a
  fresh owner run.

The immutable v4 reanalysis still reproduces the identical 17 statuses
(unchanged path).

## 3. Tests (`tests/test_cpu_share_checks.py`, `288de6df…`, 75 tests)

New `ActualV5FinalizationPathTests` (skipped unless the immutable v5 dir is
present; it is here) traverse **all real archived v5 stdout/stderr through
the production indexer** and through the production CLI (`--index-only`),
asserting: no crash; pretty-object/scalar/probe-JSONL/text shapes; integrity
true for the real set; `functional_verdict null` and the stalls visible;
and crafted temp dirs covering **null / true / number / string / array /
malformed probe lines** plus malformed pretty snapshots — every non-object or
malformed expected-probe record forces integrity FAIL with six categorized
errors, while the same JSON array on a non-probe label is fine. An
`IncrementalMetadataTests` class proves `command-metadata.jsonl` rows survive a
build failure and the indexer distinguishes every JSON value kind / empty
probe stream.

Matrix: `tests/test_cpu_share_checks.py` **75 OK**; combined discovery
**106 OK** (incl. seed `test_cpu_limit_checks.py` 31 OK; 7 real-binary
tests run with `HARDDIND_SHARE_PROBE`). `scripts/*.py` compile clean;
probe rebuilt clean under global EX (`gcc -O2 -Wall -Wextra -Werror -static
-pthread`). No probe/checker functional change this round (probe and
`cpu_share_checks.py` hashes identical to v4); seed
`src/cpu_limit_probe.c`, owner/round10/round-v5 immutable dirs and
`/root/udock` untouched.

## 4. Files

| File | sha256 |
|---|---|
| scripts/raw_output_index.py (new) | `0b613f67e03d47debbdd7c785c84d9fdcee60cbf641f9bca92b1b2ab1897963f` |
| scripts/run-cpu-shares.py | `32013f9ef987fe10d8c998a4e6e907e585c7707069e236b6144e46c2935abf3b` |
| tests/test_cpu_share_checks.py | `288de6dff4ef5ca46da7b02d60dec6f6a5342369615643885a119ab9407ee486` |
| reanalysis/v5/raw-output-index.json | `04f52f33710c8add2525e96dd4fe2d182017b68c13b426d9def1219635196ae` |
| reanalysis/v5/index-report.json | `16b83f8c74fb86f633fa7e13828f83d694766b4f2b0b0a1401b3b5e62cb3c2a5` |

Probe `3ecabf9e…` and checker `1c8073d9…` unchanged from v4; v4 replay
`scripts/reanalyze_shares_run.py` `b1d93517…` unchanged.

## 5. Owner rerun

`python3 scripts/run-cpu-shares.py <WORK> --kernel <k> --disk <d> --name <n>`
(schema 4). Every command row is now durable before finalization; any
finalization failure still leaves a complete fail report plus
`command-metadata.jsonl`, `input-hashes-{before,after}.json`,
`raw-output-index.json` and all raw stdout/stderr.
