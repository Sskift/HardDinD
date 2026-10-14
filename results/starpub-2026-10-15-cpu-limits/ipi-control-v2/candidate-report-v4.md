# CPU sampler repair (round10) — v4 measurement repair

Status: **code complete; runtime verification `not_tested` — no actual Docker /
guest / VM / GPU / performance run was started by this agent.** The owner
schedules the bounded v4 guest run with the exact command below. Nothing here
claims full CPU enforcement acceptance or near-native performance: on the
frozen v3 evidence, all fixed phases and both 2-core update windows passed, and
the 0.5-core live-update window was and remains **indeterminate** for reasons
this repair makes observable rather than green.

Scope was limited to `src/cpu_limit_probe.c`, `scripts/cpu_limit_checks.py`,
`scripts/run-cpu-limits.py`, `tests/test_cpu_limit_checks.py` in this copy. No
frozen owner copy was modified; `/root/udock` was never touched; no host
cgroups, sockets, KVM, guests or GPUs were used.

## 1. Source hashes

| File | v3 frozen (seed) | v4 (this delivery) |
|---|---|---|
| `src/cpu_limit_probe.c` | `7dd2d82d…7b2de` | `3711d135…4efce` |
| `scripts/cpu_limit_checks.py` | `d68f7c04…70d4` | `04a9824e…5b7f` |
| `scripts/run-cpu-limits.py` | `350c7bf2…1fa3` | `d88dc061…4c59` |
| `tests/test_cpu_limit_checks.py` | `cd69bbe…022d5` | `a2d8d59a…d417` |

Full hashes:

```
3711d1352a52dac4c1f64eed46aef3a0df9dbdbe14d664834268b2e13944efce  src/cpu_limit_probe.c
04a9824e8bd3e67aaf2b0851721ecbd8e85cb77ce395dbb93f7498558c6a5b7f  scripts/cpu_limit_checks.py
d88dc06101e5317ba6351d698cf05528e16a42cd746093b4739de31fbd964c59  scripts/run-cpu-limits.py
a2d8d59a4f5810866e2c2d4c68e2a29e37bfabe1c7d651febd0e540aa3d7d417  tests/test_cpu_limit_checks.py
```

Stripped static probe built during validation:
`8c5ce0b83319fc5570201839733318fa3fe1e3e4914f525d3e55dee3cf997740`
(1,147,608 bytes; byte-identical on a second build; no `INTERP`; the runner
rebuilds from source under lock and records its own hash).

## 2. Root cause from the immutable v3 evidence (read-only)

Evidence: `/root/harddind-rebuild/cpu-limits-owner-v3/run/results/cpu-limits-v3/`
(report + raw `logs-cpulim-update.stdout`, 129 samples over 20.076 s).

The v3 sampler recorded the timestamp **before** `read_cg()`, which opened and
read five files (`cpu.max`, `cpuset.cpus`, `cpuset.cpus.effective` with 8 KiB
buffers, `cpu.weight`, `cpu.stat`) — many syscalls across which the sampler
could be descheduled while quota-throttled or starved. Reading those counters
later than the stamped clock is proven by the raw data (probe-relative
seconds, whole-cgroup `usage_usec` rate between consecutive samples):

| interval (s) | Δwall | usage rate (cores) | nr_periods/s |
|---|---:|---:|---:|
| 5.287 → 5.575 | 0.29 | 1.250 | 10.4 (straddles first update) |
| 5.575 → 6.675 | 1.10 | 1.182 | 23.6 (straddles first update) |
| 6.675 → 8.675 | 2.00 | **0.145** | 3.0 |
| 8.675 → 8.975 | 0.30 | 0.400 | 6.7 |
| 8.975 → 9.175 | 0.20 | **5.401** | **110** |
| 9.175 → 11.475 | 2.30 | **0.087** | 1.7 |
| 11.475 → 11.775 | 0.30 | **1.533** | 26.7 |
| 11.775 → 12.500 | 0.72 | 0.469 | 1.4 |

- A delivery of **5.4 cores with 110 CFS periods in 0.20 s under a 0.5-core /
  100 ms quota is impossible in real time**. It means the clock for the 9.175
  sample was taken at 9.175 but its `cpu.stat`/`cpu.max` reads completed only
  ≈2.2 s later (counters +1.08 s, +22 periods), after the sampler was
  descheduled mid-`read_cg`.
- The sample stamped **11.775** already contains quota **200000**, although the
  `docker update --cpus 2.0` command was scheduled at harness t0+12.0
  (`update-1` elapsed 0.211 s; write lands ≈12.2 probe time). The 1.533-core
  interval confirms its reads finished after the transition: a transition got
  attributed to a pre-transition timestamp.
- Conversely, 6.675→8.675 and 9.175→11.475 delivered only **0.145 / 0.087
  cores** for 2.0–2.3 s with ~2–3 periods/s. CFS throttling alone can suppress
  a runnable cgroup only until the next 100 ms boundary; multi-second delivery
  far below quota means the runnable workload was not executing (host did not
  place the guest vCPUs). The outer environment was heavily loaded
  (loadavg 35.97→40.27; outer cgroup `cpu.max` is `3599999 100000`, 36 of 72
  CPUs). This is evidence of host/vCPU preemption or starvation, **not** proof
  of a kernel bug; no such claim is made.
- The probe did **not** cause the sparseness by consuming quota: the sampler
  used 420 ms / 20.08 s = **0.021 cores** in the update run and 0.003–0.031
  cores in the fixed phases. The cause is non-placement of the sleeping
  sampler between clock and reads, plus the multi-syscall window that let a
  deschedule split the timestamp from its observation.
- v3's guard was sample-count based (head 5, tail 2). Under 0.5-core
  throttling the half segment got only six samples
  (5.575, 6.675, 8.675, 8.975, 9.175, 11.475), so `samples[5:4]` was empty and
  the window was honestly reported `indeterminate` ("window too short after
  transition guards"). Sparse sampling deleted the window; it could never
  produce a false pass, but it also could not use the stable interior.

No model substitutes for this. v3 samples cannot be post-hoc repaired (they do
not contain capture brackets), and none of their 0.5-window readings are
turned into a pass.

## 3. Repair (v4)

### Probe — `src/cpu_limit_probe.c`

- **Bracketed captures.** Every spin sample records `wall_ns` immediately
  before the first read and `cap_end_ns`/`cap_dur_ns` immediately after the
  last clock. `proc_cpu_ns` is read inside the bracket. Start/end events carry
  capture brackets too. A deschedule between syscalls now widens the visible
  capture window instead of back-dating counters.
- **Stable `cpu.max` across the capture.** `cpu.max` is read **before and
  after** `cpu.stat` (`q2_*` fields, `max_stable`). If the two reads disagree,
  the observation is emitted as `max_stable:0` and the checker quarantines it:
  no setting is allowed to claim counters that may straddle a transition.
- **No silent retries (per owner WIP note).** A persistent-fd read failure
  fails that capture with the **first errno preserved** and drops the stale fd;
  the next capture reopens and resumes. Nothing is retried into a successful
  observation. Executable fixtures prove malformed/missing/resuming behavior
  through the real `capture()` (new `capture-test` subcommand).
- **Parse errors fail the read.** EPROTO from `cpu.max` parsing propagates to
  `max_errno`; two invalid reads cannot emit `max_stable:1, max_errno:0`.
- **Lower overhead.** Sample hot path uses persistent fds (`lseek(0)`+read,
  verified on this host's cgroupfs and seq files) touching only `cpu.max`
  (twice), `cpu.stat`, plus one 256-byte `/proc/stat` aggregate line. The
  cpuset/weight files are read only for start/end/snapshot/info evidence, as in
  v3. Local protocol runs show captures of ~0.35–0.6 ms.
- **Diagnostic steal channel.** `/proc/stat` aggregate steal ticks are emitted
  as `steal_ns`/`steal_errno` on samples and start/end/sample/info
  (`clk_tck`-normalized). It is strictly diagnostic: absence/error never
  changes a verdict; a reversed steal counter fails the timeline. It lets the
  owner correlate multi-second gaps with host preemption of guest vCPUs.
- v3 behavior deliberately preserved: explicit sampler CPU accounting,
  `load_deadline_ns` idle-tail handling, no `docker exec` inside measured
  windows, musl/glibc-independent fully static build, all raw fields at
  start/end, 64-thread/CPU_SETSIZE affinity handling.
- Sleep cadence uses absolute `clock_nanosleep` with EINTR restart; late
  wakeups are recorded as gaps, never caught up.

### Checker — `scripts/cpu_limit_checks.py`

- **Time-based guards replace sample-count guards**: 0.5 s head / 0.2 s tail
  measured from the last/first *stable* capture edges (same conservative
  durations v3 approximated with sample counts).
- **Stable interior requirement**: rate endpoints must be stable captures,
  fully inside the guarded range, at least 1.0 s apart, with no unstable
  capture between them (contiguous groups; the longest group is used). Slow
  captures (> 25 ms, the accounting-sliver scale) are preserved in
  `slow_indexes` and never serve as endpoints; every sample remains in
  evidence (`samples_total`, `used`, `stable/unstable/slow_indexes`,
  `candidate_groups`, `endpoint_indexes`).
- **Transition integrity**: stable captures collapsed in order must equal the
  schedule `[(0,q,p), …]`. An in-capture flip is legal only on a scheduled
  adjacency; a flip returning to the same setting, a stable key appearing
  mid-segment, reversed clocks, reversed counters (incl. steal) or any missing
  capture field is FAIL. Missing `cpu.max`/`cpu.stat` stays FAIL; nothing is
  silently defaulted.
- **Capture interval as explicit uncertainty**: for endpoints a,b the counter
  elapsed time is an interval
  `[b.wall − a.cap_end, b.cap_end − a.wall]`. Quota bounds are mathematical
  CFS bounds (`cfs_band`, unchanged constants/sliver): FAIL only when the
  *highest* plausible rate exceeds the upper bound at the shortest plausible
  interval; below-bound starvation is INDETERMINATE at the longest interval;
  proc/stat agreement is checked over the intervals. No tolerance was added.
- Fixed phases use the same wide/narrow interval treatment for process/stat
  rates; sampler CPU remains a required, separately reported part of cgroup
  consumption; missing `sampler_cpu_ns` now raises instead of silently
  dropping observer consumption.
- Under v3-shaped starvation evidence the 0.5 window remains
  **indeterminate**; sparse-but-consistent delivery (counter deltas integrate
  legitimately over irregular gaps with advancing throttle counters) can pass
  without depending on sample density.

### Runner — `scripts/run-cpu-limits.py`

- Compilation now takes the global/work `kernel-build.lock` **EX** while the
  bounded 60 s gcc build runs, then **downgrades to SH** for the guest
  lifecycle (owner `OWNER-BUILD-LOCK.md`).
- Report `params` records `measurement_revision: 4`, guard values,
  `capture_max_s`, `min_interior_s`; `update_timeline` is produced by the new
  stable-timeline check (unstable captures allowed only at transitions);
  strict missing-evidence handling and required labels are retained unchanged.
  Exit codes unchanged (0 pass / 1 fail / 2 indeterminate).

## 4. Exact bounded validation commands and results

All commands bounded ≤60 s, ≤4 jobs, no VM/guest/GPU/busy benchmark/host
changes. (`timeout` shown on the heavier ones.)

1. `timeout 60 gcc -O2 -Wall -Wextra -Werror -static -pthread
   src/cpu_limit_probe.c -o /tmp/probe-final` → clean; `readelf -l` INTERP
   count `0`; `selftest` → all parsers incl. `parse_steal` `pass`, exit 0;
   repeat build byte-identical.
2. `timeout 60 python3 -m unittest discover -s tests` → **Ran 99 tests … OK**
   (60 in `test_cpu_limit_checks.py`: the 35 v3 tests preserved and passing,
   25 new).
3. Probe `capture-test` executable fixtures (run inside the test class, which
   compiles the probe once): malformed `cpu.max` → `max_errno=max2_errno=71
   (EPROTO), max_stable=0`; missing file → errno 2 (ENOENT); malformed
   `cpu.stat` → `stat_errno=71` with stable max; malformed `/proc/stat` →
   `steal_errno=71` with verdict-relevant reads clean; two-capture resume with
   file appearing at 50 ms → first errno 2 preserved, second reopened and
   stable. Each failed sample normalizes to a checker FAIL.
4. Frozen-v3 replay through the v4 checker (read-only): all six fixed phases
   classify identically to v3 (`reference`, `cpus1`, `explicit`, `cpuset0`,
   `cpuset-range`, `cpuset-quota` → **pass**); the raw v3 update stream is
   **refused** — `fail: sampler output is missing required capture fields`
   (`cap_end_ns`, `q2_*`, `max2_errno`, `max_stable`). v3 evidence is not
   silently reinterpreted.
5. Protocol-only local spins (200–300 ms, ≤2 threads; JSONL plumbing, never
   enforcement evidence — the outer cgroup aggregates cross-tenant usage, as
   the round8 report documented): start/sample/end schema valid, all captures
   stable/tight (<25 ms), counters monotonic; real local JSONL parses through
   `spin_summary`/`normalize_update_samples`.
6. Read-only host checks: persistent-fd `lseek(0)`+re-read works for
   `cpu.max`, `cpu.stat`, `/proc/stat`; host aggregate `/proc/stat` line 73 B,
   guest-size `cpu.stat` 144 B; `/proc/stat` steal 0 on the host itself.
7. Runner failure-path smoke (temp WORK, nonexistent kernel/disk, twice incl.
   the EX→SH lock change): probe builds and self-tests under the lock,
   `instance-start` fails fast, report `verdict: fail` with per-command
   evidence retained; temp artifacts removed.

### New negative/positive tests (25 added, 35 retained)

Sparse irregular v3-shaped samples yield a valid half window at consistent
delivery; same timeline with the actual v3 starvation rates (0.087–0.40 cores
gaps) stays indeterminate; 50 ms capture excluded and its window invalid;
capture interval uncertainty turns a nominal in-band 0.625 edge into FAIL;
unstable captures at both transitions quarantined (never endpoints);
in-setting flip FAIL; stable mid-segment churn FAIL; missing `cpu.stat`;
missing capture fields; reversed capture clock/wall/counters; steal reverse
FAIL; steal absent verdict-neutral; sampler clock missing raises; fixed-phase
capture-window plumbing, reversal rejection, and steal surfacing; six
executable `capture()` fixture tests above.

## 5. Evidence boundaries / what v4 does and does not settle

- Settled in code, against frozen evidence and synthetic fixtures: timestamps
  now bracket reads; setting changes during a capture are visible and excluded;
  guards are time based; slow/missing/unstable observations are preserved and
  fail closed; quota bounds are the unchanged mathematical CFS period bounds
  with capture uncertainty; sampler accounting and idle-tail handling are
  retained.
- Not settled without an owner run: whether the live 0.5 window **passes** in a
  less loaded guest. If the host starves the vCPUs again for seconds, the v4
  window remains `indeterminate` (delivery below bound) — that is the correct
  outcome, and `steal_ns` plus capture durations will show whether host
  preemption caused it. Full Docker/CPU and performance acceptance are not
  claimed; `full_acceptance`/`performance_acceptance` remain false.

## 6. Owner v4 run command

Same prerequisites as v3 (pinned kernel, `runtime.ext4` fixture,
`client/{docker,ld-musl-…}` bundle, static glibc gcc); the runner rebuilds the
probe itself under EX lock. Choose a fresh immutable name in the owner's v4
work tree (reuse refused):

```
python3 /root/harddind-rebuild/arkclaude/round10/cpu-sampler-repair/scripts/run-cpu-limits.py \
  <v4 WORK dir> \
  --kernel /root/harddind-rebuild/kernel-build/linux \
  --disk /root/harddind-rebuild/runtime.ext4 \
  --name cpu-limits-v4-YYYYMMDD-NN --cpus 4
```

(with v3's arguments: default `--window-ms 6000 --sample-ms 100`; updates at
5 s and 12 s of the 20 s spin; bounded guest spin ≈48 s total; takes the
shared `kernel-build.lock`, EX during compilation then SH). Evidence lands in
`<WORK>/results/<name>/` as before, now including per-sample capture windows,
`max_stable`, capture-error errno fields and steal diagnostics.

## 7. Owner notes addressed

- `arkclaude/round8/OWNER-C-PARSER-REVIEW.md` (already in v3 seed): `max
  100000`, CPU_SETSIZE, missing-stat handling retained; negative fixtures
  retained/extended.
- `arkclaude/round10/cpu-sampler-repair/OWNER-WIP-CAPTURE-ERRORS.md`: EPROTO
  propagation and stable rejection; no in-capture silent retry (first errno
  preserved, resume next sample); six executable malformed/read-error fixtures
  through the real `capture()`; bounded commands.
- `arkclaude/round10/cpu-sampler-repair/OWNER-BUILD-LOCK.md`: EX lock for the
  bounded compile, SH downgrade before guest lifecycle; strict missing-evidence
  handling and required labels retained; no shares-scope expansion.
- `arkclaude/round9/docker-cpu-shares/OWNER-REVIEW-V1.md` read for context
  only (its instruction is not to touch round10 files; its fail-closed
  evidence discipline is applied here).
