# Docker `--cpu-shares` contention experiment — delegation report (round9, docker-cpu-shares)

Date: 2026-10-14 (UTC)
Status: **code complete, statically and synthetically validated; runtime NOT executed by this agent.**
The functional Docker run is owner-scheduled (the seed `run-cpu-limits.py` run itself is still queued/unvalidated). Nothing here is a product-completion or performance claim.

## 1. Goal and scope

Independent, bounded experiment proving that stock Docker `--cpu-shares`
(cgroup v2 `cpu.weight`) changes **actual runtime CPU allocation** between two
*nested* Docker containers inside the persistent harddind guest — without any
extra host privilege, KVM exposure, host socket, or host cgroup writes. Only
shared outer cgroup files are read, read-only.

Scope is deliberately CPU-shares only. No host native load is ever run; no
guest/VM/GPU was launched by this agent. `/root/udock` was not touched.

## 2. New files (only these four were added)

| File | Purpose | sha256 |
|---|---|---|
| `scripts/run-cpu-shares.py` | Owner-run evidence-collection harness (451 lines) | `56e550ca9fdba24903a270a3668d66c939ad14f27c1e48bcc6d64ec2b0f6b656` |
| `scripts/cpu_share_checks.py` | Pure-Python functional evaluator, no Docker/clocks (754 lines) | `e3368c83a15ca0a1bc90ddb3ec39e7f37b99efe7f253a8d14f0028b6300e90c1` |
| `tests/test_cpu_share_checks.py` | 44 bounded synthetic unit tests (541 lines) | `f6f5eb717a6d30d25f4f550de21d20ec027d44224f0e56aab444f944b0b7d5bf` |
| `REPORT.md` | This report | (self-referential; hash after delivery) |

Seed/common files were **not modified** (mtimes predate this session; verified):
`src/cpu_limit_probe.c` is reused **unchanged**
(sha256 `f6791d6bb1ac050933051c6fc1857bbd8abae8c206cee509eb5a44ee9fc87a82`),
as are `scripts/harddind.py`, `scripts/guest_cleanup.py`,
`scripts/run-cpu-limits.py` (`0e2388d3…c7839`) and
`scripts/cpu_limit_checks.py` (`eb1502a6…4729`). No essential bug in a common
file was found, so no modification was warranted.

## 3. Experiment protocol (implemented, awaiting owner execution)

* Guest is exactly **4 vCPUs, cgroup v2** (`--cpus 4` is enforced by argparse
  and re-checked from `docker info` NCPU/CgroupVersion). This is not a
  single-CPU product claim: the single shared CPU is the contention fixture.
* Two detached nested-Docker containers `cpushare-a` / `cpushare-b` are
  started 0.25 s apart (deliberate stagger) from a scratch image containing
  only the unchanged static `/cpu-limit-probe`. Both run
  `spin --threads 4 --duration-ms 32000 --sample-ms 100` (defaults), both
  pinned with `--cpuset-cpus 2` (default, owner-selectable in 0..3) and **no
  quota** (`cpu.max` must be `max` throughout; any `--cpus`/quota is a FAIL).
  Eight always-runnable threads contend for one guest vCPU.
* Fixed stage plan on the **same container IDs**:

  | stage | A `--cpu-shares` | B `--cpu-shares` | expected f_A = cpu_A/(cpu_A+cpu_B) |
  |---|---|---|---|
  | 0 | 1024 | 512 | 2/3 |
  | 1 | 512 (`docker update --cpu-shares`, live) | 1024 (live) | 1/3 |
  | 2 | 512 (unchanged) | 512 (live) | 1/2 (equal-weight control) |

  Stage anchors are host-scheduled 9 s apart; the spin duration
  (`5000 + 3*9000 = 32000 ms`) bounds the whole guest busy time (~32 s).
* Every stage is bracketed per container by `docker exec … snapshot` (in-guest
  read of `cpu.weight`, `cpuset.cpus.effective`, `cpu.stat`, timestamped in
  guest `CLOCK_MONOTONIC`) and `docker inspect` of `HostConfig.CpuShares`,
  `CpusetCpus`, `CpuQuota`, `Id`, `State.Running/ExitCode`. Each
  `docker update` is recorded with from/to shares, exit code and timestamps.
* After both probes exit 0: full spin JSONL logs (all samples preserved),
  per-thread `CLOCK_THREAD_CPUTIME_ID`, process `CLOCK_PROCESS_CPUTIME_ID`
  samples, cgroup `cpu.stat usage_usec`, full per-thread affinity masks
  (start/end) and `sched_getcpu()` residency sets are collected; containers are
  `docker rm`’d, a name-filtered `docker ps -aq` proves no leftovers, and the
  guest is stopped cleanly.

### Window alignment (the core anti-confounding rule)

All probe timestamps are one common guest `CLOCK_MONOTONIC`. For each stage the
scored window is the intersection, across both containers, of their post-stage
snapshot times, with **0.6 s guards** on every edge and interior-only samples:

```
lo_k = max_c(snapshot(c,k).wall_ns) + 0.6 s        hi_k = min_c(snapshot(c,k+1).wall_ns) - 0.6 s
hi_2 = min_c(last_sample(c).wall_ns) - 0.6 s
```

Thus startup stagger, `docker update`/exec edge jitter, and staggered exits can
never be scored as a weight effect. Windows shorter than 4.0 s or under 20
samples are **INDETERMINATE**, never scored. Per-window rates come from each
container’s own first/last interior sample of both `proc_cpu_ns` and
`usage_usec`.

## 4. Verdict logic and tolerance justification

A stage passes only when *all* hold (constants in `cpu_share_checks.py`):

1. **Counter agreement** — process-clock and `cpu.stat` rates agree within
   `max(0.03 cores, 5%)` (`AGREE_MIN/AGREE_REL`), and the two derivations of
   f_A agree within 0.05. Whole-run per-thread / process / cgroup totals agree
   within 0.08 cores (`WHOLE_RUN_AGREE`).
2. **Saturation** — combined delivery is in **[0.85, 1.15] cores**. Below 0.85
   the shared busy load was insufficient → INDETERMINATE (cannot demonstrate a
   weight effect); above 1.15 through a one-CPU cpuset is impossible → FAIL
   (contradictory counters).
3. **No starvation** — each container receives ≥ 0.10 cores and ≥ 0.12
   fraction, every stage; otherwise FAIL.
4. **Functional fairness band** — f_A is within **±0.09** of the weight-implied
   fraction (±0.08 for the equal stage): 2:1 → [0.577, 0.757], 1:2 →
   [0.243, 0.423], equal → [0.42, 0.58].
5. **Ordering** — f_A(2:1) > f_A(equal) > f_A(1:2), each gap ≥ 0.05, proving
   the live reversal on unchanged IDs actually reversed allocation.
6. **Weight/identity/configuration evidence** — in-container `cpu.weight`
   ordering/equality matches the plan at every stage; identical share values
   map to identical weights everywhere (this is what rejects an *unapplied
   update*: stage-1 weight staying at the stage-0 value collides with the
   shares→weight mapping seen on the other container); inspect `CpuShares`
   matches exactly; no quota; container IDs identical from creation through
   post-wait final inspect; both probe exits 0; every worker’s full affinity
   mask and residency is exactly `{shared_cpu}`.

**Why ±0.09 is a functional-fairness tolerance, not a performance threshold.**
CFS dispatches by vruntime with slice granularity `sched_min_granularity_ns`
(default 2.25 ms; 100 ms bandwidth period). A ≥ 4 s window contains well over
a thousand slices per CPU, so scheduler quantization averages below 1%. The
dominant residual errors are window-edge sample quantization (±2 of the 100 ms
samples ≈ 5% of a 4 s window), microsecond/tick rounding between the
independent counters, and transient sleeper slack around weight changes — the
latter excluded by 0.6 s guards. ±0.09 is roughly twice that residual budget:
it asks only “weights measurably steer allocation”. The expected 2:1/1:2
fractions (0.667/0.333) stay 0.167 away from 0.5 and entirely outside each
other’s bands, so reversed or unapplied configurations cannot pass. There is
**no workload-throughput acceptance criterion**; the workload is a pure spin
sink and only CPU clocks are read.

Statuses are kept separate as required: structural contradictions →
**fail**; noisy/insufficient/short evidence → **indeterminate**; work never
measured → **not_tested**; all required rows present and satisfied → **pass**.
Overall precedence is fail > indeterminate > not_tested > pass, and a missing
check row is itself a fail.

## 5. Evidence and safety guarantees

* **JSON report always saved on failure**: written in `finally`, re-parsed to
  prove serializability; only JSON-native types (explicit unit test; no Python
  sets anywhere — sets are converted to sorted lists inside the checker).
* **Raw evidence per command**: `<label>.stdout`, `<label>.stderr`,
  `commands.log`, exit codes, timeouts, elapsed/issued timestamps (UTC and
  monotonic), and full process-group cleanup audits; original exception
  strings land in `errors[]`.
* **Every required command must exit 0**, not time out, and show
  `clean_shutdown` for its private process group (34 required labels, e.g.
  `build-probe`, `instance-start`, `image-import`, `start-a/b`, all
  snapshots/inspects/updates, `wait-a/b`, `logs-a/b`, `final-a/b`, `rm-a/b`,
  `ps-leftovers`, `instance-stop`). Best-effort labels (`prerm-*`,
  `cleanup-*`, `final-stop`) are still group-audited.
* **Exact cleanup**: leftover inventory must be empty; guest final state must
  be `stopped`, not forced, with clean guest and network groups.
* **Hashes before/after** for kernel, disk, probe source, compiled probe
  binary, runner, checker, `harddind.py`, `guest_cleanup.py`; any change fails
  `inputs_unchanged`.
* All waits bounded (startup 12 s, docker calls 20–45 s, image import 120 s,
  instance start 180 s, stop 90 s, probe spin 32 s at defaults).
* The runner takes `LOCK_SH` on `work/kernel-build.lock` for the guest
  lifecycle, mirroring the seed; it never holds a lock over unbounded work.

## 6. Validation performed by this agent (static/synthetic only)

Environment: Python 3.10.12, gcc 11.4.0, Linux 5.15 host. No guest/VM/GPU, no
busy spin, no benchmarking.

1. **Probe reuse compile** under the mandated global lock (single gcc,
   bounded, full log captured):
   `flock -x /root/harddind-rebuild/kernel-build.lock timeout 55 gcc -O2 -Wall -Wextra -Werror -static -pthread src/cpu_limit_probe.c -o /tmp/cpushare-check/probe`
   → exit 0, zero warnings; `/tmp/cpushare-check/probe selftest` →
   `{"status":"pass", …}`.
2. **Checker unit tests**: `timeout 60 python3 tests/test_cpu_share_checks.py`
   → **44 tests, OK** (ran in ~0.1 s, single process). Includes the required
   negative synthetic cases, each asserting non-pass:
   opposite-weight allocation FAIL; opposite observed `cpu.weight` FAIL;
   unapplied live update FAIL (same shares → two weights / wrong ordering);
   equal stage with unequal weights FAIL; empty/too-short window
   INDETERMINATE; insufficient shared busy load INDETERMINATE; starvation FAIL;
   proc/stat counter disagreement INDETERMINATE; >1 core through cpuset FAIL;
   missing command / nonzero exit / timeout / dirty process group / optional
   label leak / missing update record / failed update FAIL; never-collected
   leftover inventory FAIL; forced shutdown / changed inputs / cgroup-v1 guest
   / non-4-vCPU guest / identity change / nonzero probe exit / residency or
   affinity escape / quota present / missing sample field / non-monotonic
   snapshot time / parse-error FAIL; plus precedence/JSON-native tests.
3. **Seed regression**: `timeout 60 python3 tests/test_cpu_limit_checks.py`
   → **31 tests, OK** (unchanged). Combined discovery
   `python3 -m unittest discover -s tests -p 'test_cpu_*.py'` → **75 tests, OK**.
4. **Real-probe parser cross-check** (read-only `info`/`snapshot`, no spin):
   checker parses the actual probe JSONL, reads integer `cpu.weight` (host
   value 100) and all five cgroup keys, monotonic `wall_ns`.
5. **Runner CLI guards** verified without launching anything: bad name,
   `--cpus 1`, out-of-range `--shared-cpu`, bad `--phase-ms` all exit with
   argparse errors before any side effect.
6. **Failure-path integration** (throwaway temp WORK, a fake `gcc` that exits
   1, no guest): runner returns 1, `report.json` is always written and
   re-parseable, verdict `fail`, with `not_tested` phase rows, `indeterminate`
   probe-stream row, failed command ledger/lifecycle rows, passing
   inputs-unchanged row, and the raw synthetic stderr preserved in
   `build-probe.stderr`. Artifacts were removed after the check.

## 7. Owner execution (runtime; deliberately not run by this agent)

```
python3 scripts/run-cpu-shares.py <WORK> \
  --kernel <path-to-kernel> --disk <path-to-runtime.ext4> \
  --name <immutable-lowercase-name> \
  [--cpus 4] [--shared-cpu 2] [--threads 4] [--phase-ms 9000] [--sample-ms 100]
```

Output: `<WORK>/results/<name>/report.json` plus per-command stdout/stderr and
an `instance/` state snapshot. Process exit: 0 pass, 1 fail, 2
indeterminate/not_tested. Expected wall time ≈ guest boot + ~1 min of bounded
operations + shutdown, ~32 s of which is guest-internal busy spin. Schedule
under the owner runtime queue and the global `/root/harddind-rebuild/kernel-build.lock`
protocol.

## 8. Honest pending-runtime boundary

Not yet observed (only the owner runtime can produce them): actual
`docker update --cpu-shares` latency/effect inside the UML guest, the actual
kernel mapping of shares 1024/512 to numeric `cpu.weight` (recorded raw; the
checker requires only ordering/equality consistency, so it is robust to the
exact v1→v2 conversion), real f_A distributions on the pinned vCPU, and clean
guest lifecycle on this image. A runtime PASS is therefore **not** claimed.
The queued seed CPU-limit run remains independently unvalidated; this
experiment does not depend on its result beyond sharing the same harness
primitives. If a real run yields INDETERMINATE windows (e.g. boundary overhead
shrinks stage 2 below 4 s), the owner can rerun with larger `--phase-ms`
(5000..20000); FAIL rows point at the exact contradicted invariant with
evidence attached.
