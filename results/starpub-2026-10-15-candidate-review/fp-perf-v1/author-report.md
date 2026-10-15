# REPORT-PERF-HARNESS-v2 — corrections to the owner-run perf supervisor

Date: 2026-10-15. Supersedes the harness code in v1 (v1 is preserved
immutable by the owner at `/root/harddind-rebuild/fp-perf-owner-review-v1`,
canonical layout `source/` + `report.md`). All six review items in
`OWNER-PERF-HARNESS-REVIEW.md` are repaired below and proven with parser
fixtures plus **tiny fake executables driving the real supervisor paths
with real Popen** — no benchmark, guest, kernel, VM or GPU was run by me;
the owner schedules all measurements. A skipped/failed test is never a pass.

## File hashes (v2)

| file | sha256 |
|---|---|
| `fp-perf-supervisor.py` | `0482ad62…5e7b` |
| `perf_log.py` | `37bdac08…267f` |
| `harddind-perf-init` | `eaf690e6…cf25` (unchanged from v1) |
| `perfrun.c` | `ffbbf087…b43d` |
| `fp-perf-prep.sh` | `72ad6f64…00c0` |
| `test_perf_log.py` | `14c80f64…8d2c` |
| `test_supervisor_paths.py` (new) | `fd009af4…8f01` |

Built artifacts (compile-only prep, this folder `perf/work`):
corebench `f43b2ece…a73e` and dispatchbench `de3b122a…3758` unchanged
from v1 (identical frozen compile); perfrun `36cd08ec…8a0b`; disk
`39ac2de0…5aea`; `perf-manifest.json` additionally pins all harness
files and the imported cleanup library
`repo:guest_cleanup.py` `569ddc41…bc14`. Supervisor `verify_inputs()`
fails the run on any drift in sources, kernels, binaries, disk, harness
files, or the cleanup library, before and after the run.

## Review items and exact tests

### 1. run_native binary-mode TypeError — fixed; tested with real Popen

v1 opened native logs with `"xb"` and `emit()` wrote `str`, so the first
BEGIN raised `TypeError`. v2 opens text mode (`"x"`). Proven by
`test_run_native_happy`: builds script fakes named `corebench` and
`dispatchbench` in a temp `work/bin`, calls the actual
`Supervisor.run_native()` — real `subprocess.Popen` through
`taskset -c 0-3`, 35 launches — and asserts rc 0, zero parser errors,
5 timed records per workload, 35 `RC=0` markers, BEGIN/END. This test
cannot pass on the v1 code.

### 2. Parser strictness — fixed; 24 parser fixtures

`perf_log.py` now enforces, in addition to the v1 marker/count rules:
- expected workers per workload keyed by `(binary, workload)` from the
  frozen binaries: **syscall 1, signals 1, threads 4, processes 4,
  dispatchbench thread_syscalls 4** (`dispatchbench` hard-sets
  `workers=4` internally regardless of argv);
- `seconds` must be a plain int/float (**bool rejected**), finite
  (NaN/±inf rejected), and **strictly positive**;
- `workers` must be a plain positive int (bool rejected) and equal the
  expected value;
- schema confirmed from the frozen sources, not invented:
  `corebench.c` prints exactly `{"test","workers","seconds"}` — a
  `verified` key on corebench is rejected; `dispatchbench.c` prints
  `…,"verified":true}` for every mode — missing or non-true `verified`
  is rejected;
- JSON outside a RUN block is rejected;
- ordering is a phase machine: RUN/SHA/RC/JSON/END before BEGIN rejected;
  SHA only once between BEGIN and the first RUN (misplaced SHA rejected);
  nested RUN without RC, RC without a RUN, duplicate RC, END inside a run,
  duplicate BEGIN/END, END before completion all rejected.

Tests (in `test_perf_log.py`, 24 cases): valid log; hash mismatch/missing;
misplaced SHA; rc124 timeout; missing run; malformed JSON; JSON outside
RUN; bool seconds; bool workers; zero/negative/non-finite seconds; wrong
workers for threads and syscall; missing/false `verified`; corebench
`verified` forbidden; test-name mismatch; no/duplicate BEGIN; duplicate
END; END inside a run; stray RC; missing JSON; nonzero END.

### 3. summarize() on budget cells; ratios exclude errored cells — fixed

`med()` no longer indexes missing `stats`; a cell participates in a
ratio only when `cell_ok()` holds: `errors == []`, clean shutdown, not
timed out, non-empty stats. `not_run_budget`/`not_run_lock_deadline`
cells (which intentionally carry no usable stats) are listed in
`ratios.excluded_cells` and omitted everywhere; the markdown prints
`EXCLUDED/-`. Proven by `test_summary_exclusions` (good cells → ratios
present; one errored cell → only that cell's cross-products disappear;
budget cell → excluded, no exception, descriptor flag preserved).

### 4. Bounded locking and real deadlines — fixed

- locks are nonblocking `LOCK_NB` attempts limited by the **remaining
  global budget** (`acquire()` raises `LockDeadline`); the driver records
  the cell as `not_run_lock_deadline`, an explicit failure, not a skip,
  and continues;
- every launch receives `min(requested deadline, remaining global
  budget)`;
- native cells: each invocation gets
  `min(run_timeout, remaining cell deadline)` and nothing launches once
  the cell deadline is reached — the v1 overrun of up to one extra 45 s
  per cell is impossible. Proven by `test_lock_deadline` (held lock,
  ~1 s synthesized budget → `LockDeadline` in <3 s) and
  `test_run_native_timeouts_bounded` (fake that ignores TERM and sleeps;
  4 s cell deadline, 1 s run timeout → measured wall < 9 s, timeout
  failures recorded, cell marked timed out).

### 5. Cleanup identities, interruption, full guest group — fixed

- `launch_one()`/`launch()` run `cleanup_group()` in `finally` (SIGKILL on
  any exception too), fsync a `cleanup` event with initial/remaining live
  and zombie pid identities, and always close both streams;
- guest `perfrun` timeout path now kills the **whole process group**:
  SIGTERM to `-pgid`, 2 s grace with leader reaping plus a `/proc` pgrp
  census, then repeated SIGKILL to `-pgid` until zero non-zombie
  survivors (bounded), returning 124 — covers fork()ed/exec()ed children
  that outlive or ignore the leader's TERM.
  `test_launch_kills_whole_group` runs a real `sh -c 'sleep 30 & wait'`
  under the actual `launch()` with a 1 s deadline and asserts
  `timed_out` and empty remaining live/zombie lists.

### 6. Provenance and timing observers — documented, pinned

- provenance pinned in `perf-manifest.json` and re-checked around the run:
  supervisor, parser, guest init, perfrun source, both test files, prep
  script, and the frozen matrix cleanup library
  (`/root/harddind-rebuild/fp-matrix-owner-v1/repo/scripts/guest_cleanup.py`
  `569ddc41…`), plus the two frozen benchmark sources and the vmlinux
  hashes.
- timing observers: the result number in every record is the benchmark's
  **own** `clock_gettime(CLOCK_MONOTONIC)` around its fixed loop
  (`corebench.c:11-15`; `dispatchbench.c:21-25`). In the native bracket
  that reads the host monotonic clock; inside the guest the identical
  binary reads the UML guest monotonic clock (UML timer subsystem, time
  travel off), so the two domains are compared descriptively via ratios,
  not treated as one clock. The supervisor's `time.monotonic()` is used
  solely for deadlines and `perf-events.jsonl` timestamps, never as a
  result. Pinning is host `taskset 0-3` only; the host runs `schedutil`
  with no frequency pin, which the native brackets/E,L,L,E order exist to
  expose.

## Test evidence I actually ran (fake/compile only)

Under the global EX lock, hard `timeout 170`, ≤4 jobs:
- `python3 -m unittest test_perf_log.py` → **24 tests OK** (~0.02 s);
- `python3 -m unittest test_supervisor_paths.py` → **6 tests OK**
  (~8 s; real Popen/taskset/cleanup, instant shell fakes, one 4 s
  bounded-timeout case, one 1 s group-kill case, one <3 s lock case);
- `fp-perf-prep.sh ./work 45` rebuild under EX → compile clean
  (`-Werror`), manifest above.

Nothing here is a measurement. Kernel hashes, workloads and iteration
counts are unchanged. Owner command stays:

```
cd /root/harddind-rebuild/arkclaude/round13/fp-v4-review/perf
python3 fp-perf-supervisor.py --work ./work     # work already prepped
```

After a harness edit, prep intentionally refuses to overwrite an existing
work; use a fresh `--work` directory (inputs then hash-pin the new code).

## Limits

- Fake-process tests exercise supervisor mechanics, not benchmark content;
  guest-side `perfrun` group reaping is compiled and source-reviewed but
  its guest `/proc` behavior is exercised only when the owner boots.
- Descriptive only: no threshold, tolerance, near-native, or perf
  pass/fail exists anywhere in the code; controls remain unpooled.
