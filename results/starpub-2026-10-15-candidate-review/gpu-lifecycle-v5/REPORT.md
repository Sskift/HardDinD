# Round 11 — CUDA lifecycle probe (multi-card / thread / process / memory)

## v5 — actual-native v4 failure repair

Owner froze v4, fixed two review defects there, then ran the real
four-card native probe again (`/root/harddind-rebuild/gpu-lifecycle-owner-v4/`,
immutable): shared cycles all compute/validate, multicard fine, but the
run failed (`native.stderr`, `native-verdict.json`). Every concrete
actual-native-v4 defect is fixed; owner fixes and prior protocol
hardening are retained. Owner snapshots were not modified;
`src/cuda_probe.c` is untouched.

Owner v4 fixes kept: `current_match` is emitted as a real JSON boolean
(`%s` true/false; owner fixture `tests/owner_shared_json.c` retained,
now built/run by `tests/run_host_fixtures.sh` with log
`build/shared-json.log`), and the fixture duplicate-key case uses a
separate suffix buffer (no overlapping `snprintf` src/dst).

1. **detach_rc=201 on real 550.120 (shared phase)** — clearing the
   thread's current context before `cuCtxDetach` was wrong: detach
   operates on the current context. New order is `cuCtxDetach(ctx)`
   *while current*, then `cuCtxSetCurrent(NULL)`, then
   `cuCtxGetCurrent()` verifies the clear (a still-bound context after a
   reported-success becomes `unset_rc=201`). Both rcs remain emitted and
   checker-required; a source-order test pins detach-before-clear.
2. **Children printed usage and exited before CUDA** — the top-level
   `main()` argv whitelist accepted `--gate-fd/--out-fd` under
   `--child` but not the handshake's `--rdy-fd/--rel-fd`, so all four
   execed children hit `usage()` (rc 2) before `child_main`. Both flags
   are whitelisted and the usage text documents them. Gate EOF still
   returns the specific pre-cuInit rc 3.
3. **Parent died SIGPIPE (exit -13) before summary/reap** — children
   exiting early closed their release pipes and the parent's release
   write killed the process. `main()` now installs
   `signal(SIGPIPE, SIG_IGN)` before any fork; all gate/release writes
   go through `write_pipe_byte()`, EPIPE is recorded
   (`child_gate_write`/`child_release_write`) instead of terminating,
   and the existing bounded deadline + exact-pid reap + emit/summary
   completion path always runs. Child-side ready/report writes likewise
   surface EPIPE as return codes, not signals.

### v5 GPU-free tests on the REAL paths (not just helpers)

New `RealBinaryChildPathTests` exec the compiled
`build/cuda-lifecycle-probe` with real pipes (`pass_fds`); dlopen of
libcuda at startup is the only driver interaction — no cuInit/GPU call
occurs because the gate read comes first:

- gate EOF with the full `--rdy-fd/--rel-fd/...` argv → exit **3**,
  empty stderr (no usage, no cuInit message) — proves top-level dispatch
  reaches `child_main` and the v4 argv rejection is gone;
- unknown child flag → usage rc 2; omitted `--rel-fd` → rc 2; parent
  unknown flag → rc 2; gate EOF with `--uuid` extra still reaches rc 3;
- C fixture `closed_pipe_write`: with SIGPIPE ignored
  `write_pipe_byte()` on a closed reader returns `-1/EPIPE` (errno 32),
  live-pipe round trip works;
- source tests pin `signal(SIGPIPE, SIG_IGN)` in `main()` before the
  child phase and detach-before-clear ordering.

### v5 host-only verification (bounded `kernel-build.lock`, ≤4 jobs, no GPU/VM)

- strict probe compile: `build/compile.log` `gcc rc=0`;
- `tests/run_host_fixtures.sh` builds (single sequential gcc per source)
  and runs shared-json / protocol-regression / ASan+UBSan host fixture:
  `build/shared-json.log`, `build/protocol-regression.log`,
  `build/host-fixture.log` — all failures 0, `closed_pipe_write`
  `epipe:true`;
- `python3 -m unittest tests.test_gpu_lifecycle_checks.py`: **66 tests
  OK** (`build/selftest.log`), including the six real-binary child
  dispatch tests.
- Immutable hashes: `SOURCE-MANIFEST-V5.json`. The real four-card native
  run, inventory-after and internal/VM acceptance remain the owner's
  serialized steps; no partial row is claimed as shared/multicard/full
  pass.

---

## v4 — actual-native v3 failure repair

Owner ran the strict build, ASan/UBSan fixture and 50 Python tests (all
passed), then the real four-card native run failed
(`/root/harddind-rebuild/gpu-lifecycle-owner-v3/`, immutable;
`OWNER-ACTUAL-NATIVE-V3.md`). All four actual native failures are fixed;
owner protocol hardening is preserved. The v3 archives were not touched.

1. **cuCtxAttach 201 INVALID_CONTEXT (shared phase)** — attach operates
   on the calling thread's current context, so the thread now binds with
   `cuCtxSetCurrent(ctx)` first, then `cuCtxAttach`, then verifies the
   attached handle via `cuCtxGetCurrent() == ctx` (`current_match`).
   Transitions are serialized by a dedicated mutex; teardown clears the
   thread current context before `cuCtxDetach`. Every code
   (`enter_rc`, `attach_rc`, `unset_rc`, `detach_rc`) and the identity
   match are emitted and independently required by the checker.
2. **Child wire parse failure (raw trailing newline)** — the production
   parser now takes the exact raw wire record: exactly one terminating
   `\n`, no stripping. `parse_child_line` is a strict tokenizer — unique
   known header keys, typed bounds per key, exact cycle count, one `c=`
   aggregate, report `seq` must match the pipe owner, UUID charset/shape.
   Missing/newline/extra newline/trailing junk/second record/unknown or
   duplicate keys are all rejected. Host fixture feeds the raw line
   exactly as production does.
3. **Per-cycle timestamps were never serialized** — the cycle token is
   now 16 fields ending with monotonic `st,en` (int64, `en > st`,
   bounded within the reported worker interval); `CHILD_CYCLE_FIELDS`
   and size proofs were updated (`CHILD_CYCLE_TOKEN_MAX 216`,
   `CHILD_LINE_MAX 7240`, fixture-measured worst case 206 B / 6844 B).
4. **Negative compute overlap from pre-JIT gate** — children get a
   ready/release handshake around CUDA setup: setup gate →
   `cuInit`/context/JIT → one-byte ready on a dedicated pipe → parent
   waits (bounded, handles bad byte/EOF/partial readiness) → compute
   release stamped before write → compute → report. The release unblocks
   all children even on partial-ready failure; the deadline and
   exact-pid SIGKILL reap still bound everything. Checker requires
   `fork ≤ ready ≤ release ≤ cycle start < … ≤ firstbyte ≤ wait` and
   positive overlap; new negative tests cover each violation.
5. **Protocol hardening preserved/extended** — owner's strict
   u32/int32 field bounds, exact comma count, leading/trailing comma and
   overflow rejection are kept; int64 timestamp bounds added.
   `tests/owner_protocol_regression.c` is the owner regression adapted
   to the 16-field token (max-width preservation plus overflow/count/
   comma/non-numeric/non-monotonic rejection), built by
   `tests/run_host_fixtures.sh` and cross-checked from Python.

### v4 host-only verification (no GPU/VM; author ran none)

- strict compile under `kernel-build.lock` (single `gcc`, ≤60 s flock):
  `build/compile.log` — `gcc rc=0`;
- `tests/run_host_fixtures.sh` (ASan+UBSan, two bounded compiles):
  `build/protocol-regression.log` (`failures:0`, all malformed tokens
  rejected, max token preserved) and `build/host-fixture.log`
  (`failures:0`; NaN/±inf/negative/OOB validation deterministic with no
  UB; seed table/matrix; raw-newline parser matrix; report bounds fit);
- `python3 -m unittest tests.test_gpu_lifecycle_checks.py`: **57 tests
  OK** (`build/selftest.log`).
- Immutable hashes: `SOURCE-MANIFEST-V4.json`. Real four-card native and
  internal/VM acceptance remain owner-serialized steps.

---

## v2 — owner WIP review fixes (frozen v1 + five findings)

The v1 snapshot at `/root/harddind-rebuild/gpu-lifecycle-owner-review-v1`
is untouched. All five findings from `OWNER-WIP-COMPUTE-REVIEW.md` are
fixed in this tree:

1. **Grid divisor** — launch grid is now `(elements + 255u) / 256u` to
   match the 256-thread block (was `/255u`, a harmless but real
   over-launch); a source-level test pins both sides of the expression.
2. **Validation UB** — new `validate_outputs()` compares every returned
   float against the exact f32 expected value and checks
   `isfinite`/range **before** any float→unsigned conversion; the
   checksum accumulates only matched bounded expected values. NaN,
   ±infinity, negative and out-of-range data fail deterministically.
   Proven on the actual C path under ASan+UBSan by
   `tests/cuda_lifecycle_host_fixture.c` (built/run via
   `tests/run_host_fixtures.sh`, no CUDA, no GPU).
3. **Cross-process seed namespace** — the process-global counter is gone.
   Each cycle now derives a deterministic seed from an explicit identity
   `(role, proc, worker, round, cycle)` (parent proc 0; child
   proc = seq+1), reproduced bit-for-bit by `cycle_seed()` in
   `gpu_lifecycle_checks.py`; every cycle record carries the identity and
   seed, the checker re-derives and requires them, and requires run-wide
   seed uniqueness across every phase and every exec child — the v1
   post-fork counter reset (identical patterns per process) can no longer
   pass. The C fixture prints a seed table that the Python tests
   cross-check against the Python implementation; no pid/time entropy is
   used.
4. **Invalid UUID path** — inventory UUID bytes are zero-initialized; a
   failed `cuDeviceGetUuid` emits the all-zero marker with explicit
   `"uuid_valid": false`, never a fabricated UUID, and the checker
   rejects any inventory record whose `uuid_valid` is not true (missing
   flag also fails).
5. **Child report bounds/edge cases** — report formatting moved into
   `format_child_line()` which fails closed (-1, child exit 9) without
   truncation; `CHILD_LINE_MAX` is 7240 (v4; was 5808 in v2) with a `_Static_assert` against
   fixture-verified worst-case margins (measured: 280 B prefix, 165 B
   token, 5532 B at MAX_CYCLES=32). The executable fixture verifies fit,
   tiny-buffer rejection, and rejection of truncated/count-mismatched/
   garbage wire reports (identity restoration verified on a 32-cycle
   round trip). Bounds are now honest: children sentinel `-1` means auto
   (resolved only after enumeration, re-validated against
   MAX_CHILDREN=MAX_GPUS=32), duplicate `--gpus` ordinals are rejected,
   and the post-fork host-failure path no longer serializes
   uninitialized records.

New/changed artifacts in v2: `src/cuda_lifecycle_probe.c`,
`scripts/gpu_lifecycle_checks.py`, `scripts/run-gpu-cuda.py` (link
`-lm` added only for the lifecycle build),
`tests/cuda_lifecycle_checks.py` (50 tests, all parser/evidence-based),
`tests/cuda_lifecycle_host_fixture.c`, `tests/run_host_fixtures.sh`.
`src/cuda_probe.c` remains byte-identical to the v1/accepted copy.

Status: **code complete, statically compiled, ASan/UBSan host-fixture and
parser-tested only. No GPU/VM/benchmark execution has been performed by
this session; runtime pass is not claimed.** The owner runs the
serialized real-GPU/VM commands below and keeps the native baseline +
internal-acceptance gate.

---

### v1 delivery notes (superseded where the v2 section differs)

## What this round adds

| File | Purpose |
|---|---|
| `src/cuda_lifecycle_probe.c` | New standalone driver-API lifecycle probe |
| `scripts/gpu_lifecycle_checks.py` | Independent JSONL validator (parser, not a kernel mock) |
| `tests/test_gpu_lifecycle_checks.py` | 36 parser/validator/contract unit tests |
| `scripts/run-gpu-cuda.py` | Adds explicit `--probe {compute,lifecycle}`; **default `compute` is unchanged** |
| `docs/cuda-12.4-driver-api-signatures.txt` | Verbatim CUDA 12.4 prototype evidence |

`src/cuda_probe.c` is **unchanged** (hash below). Nothing under `/root/udock`
was touched; no host privileges, cgroup writes, KVM, sockets or unrelated
CPU/network/kernel sources were modified.

## What the probe does (and does not prove)

`cuda_lifecycle_probe.c` dlopens `libcuda.so.1` (no CUDA toolkit), resolves
driver entry points with `dlsym`, and JITs the same pinned PTX already used
by `cuda_probe.c`: `.version 7.0`, `.target sm_70`. Driver target 550.120.

Phases, emitted as one JSON object per line in fixed order:

1. `meta` — requested parameters and `cuInit` rc.
2. `inventory` — every enumerated card: ordinal, UUID
   (`GPU-xxxxxxxx-…`), name, total bytes.
3. `solo` — per card, 2 repeated context create/destroy rounds, each with
   8 bounded alloc/HtoD/kernel/DtoH/exact-CPU-compare/free cycles with a
   different pattern seed every cycle. Per-round `cuMemGetInfo` free bytes
   are recorded at context entry/exit.
4. `shared` — 4 pthreads on one context: deprecated `cuCtxAttach`/
   `cuCtxDetach` usage-count pair + per-thread `cuCtxSetCurrent`, private
   1 MiB × 3 buffers per worker, mutex/cond start gate (a
   participant-count-fixed gate, not a fixed-count barrier, so a failed
   `pthread_create` cannot deadlock), monotonic per-worker intervals.
5. `multicard` — one pthread per card, each with its own fresh context and
   private module, same gated overlap evidence.
6. `child` — 4 concurrent `fork()`+`execl()` children (one per card on the
   4×A100 host; default `max(4, cards)`). The post-fork child uses only
   async-signal-safe calls (`close`, `execl`, `write`, `_exit`) and never
   touches CUDA before exec; the execed program calls `cuInit` fresh,
   verifies the assigned GPU UUID, and reports over a pipe. A one-byte
   gate starts all children together. The parent bounds runtime with
   `poll`/deadline, records fork/release/first-byte/wait timestamps and
   child exit status, and signals only its own exact child pid if overdue.
7. `summary` — final record; **the validator ignores its status and
   re-derives the verdict from the individual records.**

Every cleanup return code is captured and must be zero: the three
`cuMemFree_v2` calls per cycle, `cuModuleUnload`, `cuCtxDestroy_v2`,
attach/detach for the shared context, plus the per-child create/load/
unload/destroy codes. Compute validation is exact against the CPU-side
float expression (all values integer-valued and < 2^24, so f32 addition
matches bit for bit); any mismatch fails the cycle and the run.

Footprint: 1 MiB float arrays, three per worker; 8 cycles (2 in children);
default 30 s command deadline; workers bounded (threads/children ≤ 16).

Explicit non-claims:

- free-VRAM deltas on a shared GPU **do not** establish physical-leak
  absence; memory pressure, UVM, IPC handle and asynchronous-copy
  lifecycle suites are still outstanding;
- the guest path in `run-gpu-cuda.py --probe lifecycle` is still **manual
  device-node pass-through, not stock Docker `--gpus` integration**;
  `full_acceptance` stays `false`, and stock `--gpus` needs the separate
  final suite;
- timings are coexistence/lifecycle trace evidence under LD_PRELOAD/strace;
  there is no performance threshold and no speed claim;
- a passing lifecycle run does **not** mean the HardDinD GPU goal is
  complete. The known guest UVM-node failure and mapping candidate 0024
  remain owner-repair items; the lifecycle docker phase is expected to
  keep failing until that lands, and the runner preserves the first
  failure with raw logs.

## CUDA 12.4 signature verification

All driver signatures were checked 2026-10-14 against the primary NVIDIA
CUDA 12.4 archive docs (HTML fetched over HTTPS) and cross-checked against
symbols exported by the installed `/lib/x86_64-linux-gnu/libcuda.so.1`
(driver 550.120). Evidence list:
`docs/cuda-12.4-driver-api-signatures.txt`, including:

- `cuInit(unsigned int Flags)` — `group__CUDA__INITIALIZE`
- `cuDeviceGet`, `cuDeviceGetCount`, `cuDeviceGetName`, `cuDeviceGetUuid`,
  `cuDeviceTotalMem_v2` — `group__CUDA__DEVICE`
- `cuCtxCreate_v2`, `cuCtxDestroy_v2`, `cuCtxSetCurrent`,
  `cuCtxSynchronize` — `group__CUDA__CTX`
- `cuCtxAttach(CUcontext*, unsigned int)`, `cuCtxDetach(CUcontext)` —
  `group__CUDA__CTX__DEPRECATED` (still exported by 550.120)
- `cuMemAlloc_v2`, `cuMemFree_v2`, `cuMemcpyHtoD_v2`, `cuMemcpyDtoH_v2`,
  `cuMemGetInfo_v2` — `group__CUDA__MEM`
- `cuModuleLoadData`, `cuModuleUnload`, `cuModuleGetFunction` —
  `group__CUDA__MODULE`
- `cuLaunchKernel(...)` — `group__CUDA__EXEC`

## Validator acceptance (independently re-derived)

`gpu_lifecycle_checks.analyze(content, request)` requires, per phase log:

- exact canonical record order: meta → inventory per card → solo workers
  → shared ctx + shared workers → multicard workers → child workers →
  summary; no extra/missing/duplicated records;
- inventory UUID set/order equals the requested set (the native-before
  inventory defines the set used for docker and native-after);
- all context rounds/cycles present with per-cycle alloc/HtoD/launch/
  sync/DtoH/free rcs zero, mismatches zero, seeds distinct within a worker;
  alloc/free tallies equal 3 × cycles (× rounds);
- actual cleanup success: free/module-unload/context-destroy/detach rcs
  zero in every phase;
- positive measured overlap windows for the shared, multicard and child
  groups (`min(end) − max(start) > 0`), unique thread tids and child pids,
  child `ppid` equal to the probe pid;
- children: launch-before-reap ordering (`max(fork) < min(wait)`), gate
  timing order, report received, exec errno 0, exit 0, not signalled, not
  killed by the parent, full UUID coverage;
- the summary is only checked for consistency; it can never forge a pass.

Negative coverage in `tests/test_gpu_lifecycle_checks.py`: forged
summary-only, tampered summary, omitted GPU inventory/UUID, omitted
shared/multicard/child worker, missing phase, missing cycle, failed free
or context destroy or child unload rcs, duplicate inventory UUID,
duplicate worker id, duplicate thread identity, serial (non-overlapping)
thread timing, serial child timing, reap-before-all-launches, child
non-zero exit / signal / parent-kill, wrong ppid, missing child card
coverage, error record, reordered records, malformed JSON line, repeated
seeds, wrong element size, and every parameter mismatch
(cycles/threads/ctx-rounds/child-cycles/children), plus CLI exit codes and
runner-contract assertions (compute default + old source hash coverage).

## Runner extension (`scripts/run-gpu-cuda.py`)

- new `--probe {compute,lifecycle}` flag, **default `compute`**;
- compute mode builds/hashes/executes exactly the old `cuda-probe` path
  (same inputs, including unconditional `src/cuda_probe.c` and
  `src/cuda_ioctl_trace.c` hash coverage, same limits/cleanup);
- lifecycle mode builds `cuda-lifecycle-probe` (`-pthread` added only in
  that branch), adds `src/cuda_lifecycle_probe.c` and
  `scripts/gpu_lifecycle_checks.py` to hashed inputs, validates each
  native-before/docker/native-after log independently with the checker
  against the native-before UUID set, records per-phase verdict reasons
  and overlap windows, and keeps `kind=nested_cuda_lifecycle_diagnostic`,
  `full_acceptance=false`, the existing exact `cleanup_group`, raw
  per-label stdout/stderr/commands logs, and first-failure preservation;
- the UVM alias/standard-node discovery from e254180 is untouched;
  lifecycle runs still surface the current guest UVM failure until the
  mapping repair lands.

## Build and test commands actually run here (static, bounds respected)

Compile under the global lock (each flock attempt bounded to ≤ 60 s,
`gcc` ~1 s, no GPU):

```
flock -x /root/harddind-rebuild/kernel-build.lock -c \
 'gcc -O2 -Wall -Wextra -Werror -std=gnu11 -pthread \
  src/cuda_lifecycle_probe.c -ldl -o build/cuda-lifecycle-probe'
```

Parser/model self-tests (no CUDA, no threads, no forks):

```
python3 -m unittest discover -s tests -p 'test_gpu_lifecycle_checks.py' -v
# Ran 57 tests ... OK (v4; 50 in v2)
```

Executable C-path negative fixture (ASan+UBSan; no CUDA calls, no GPU):

```
tests/run_host_fixtures.sh     # locked compile (<=60s flock, one gcc)
                               # then runs build/cuda-lifecycle-host-fixture
```

Raw logs: `build/compile.log`, `build/selftest.log`,
`build/host-fixture.log`.

## File hashes (sha256, as delivered)

```
59a5505d8f811c12feacdabd848d42383b94232d0252d2e0b80f62bf5c52596c  src/cuda_probe.c (unchanged; identical to the pre-existing main copy)
f4aedaf65e9ac1972a5994491259420b71e4d3d73f5d76de38971446e4f99e1f  src/cuda_lifecycle_probe.c
5e669aa21f5dd0ad0e41496177fc0989bd4227a4b24ac495ac2c70ad9e454246  src/cuda_ioctl_trace.c (unchanged)
489c0ed3afd33d974e4e31ae07fbcca823ed0ac08213348d6aa3f53414ba025e  scripts/run-gpu-cuda.py
7cec6c5d4ee4f1774b5d4ef6cfd73de054e1ae96c1955a67dc9ae046d183ce14  scripts/gpu_lifecycle_checks.py
2b2fd936d570692c6c5cb67760792a6be0f215c2352f9986ab66652eec239c51  tests/test_gpu_lifecycle_checks.py
3423fd773e37c772bf5579d0bb4d9663e7d0a2aa2c3f860ffe30d14f891262c7  docs/cuda-12.4-driver-api-signatures.txt
116de7fd0289f4e25adf43a7d34c89cd84fe286bee442b2ba079ef68ae5afb3e  build/cuda-lifecycle-probe
df80b44af6f76a51dfc1566dbf0ff05a62a825f7b62644d0078304f994733cbc  build/compile.log
22dd2d55f037d610cfe72d9fba766d28b1b9a72e142df98c9e51ddc4064326ef  build/selftest.log
107466901e768ae0a4ff827a06162f6af0e51218bd5ccf67c48d7e327561a44f  /lib/x86_64-linux-gnu/libcuda.so.1 (host 550.120, runtime dependency)
```

The binary links only against glibc and resolves CUDA purely at runtime
via `dlsym`; it is not linked against `libcuda` and needs no CUDA toolkit.
The runner additionally captures hashes of source/harness/checker/binary
and every bundled library in each phase's `report.json`
(`sha256`, `library_sha256`, `inputs_unchanged`, `libraries_unchanged`) at
owner execution time; native-before/docker/native-after remain separate
phases and never collapse into one acceptance flag.

## Owner execution (serialized, real GPU + VM only — not run here)

From the owner-integrated repo root, after main integration:

```
# 1. Parser tests + executable C-path host fixture (host, no GPU)
python3 -m unittest discover -s tests -p 'test_gpu_lifecycle_checks.py' -v
tests/run_host_fixtures.sh

# 2. Existing compute baseline must remain exact (default probe)
python3 scripts/run-gpu-cuda.py <work> --kernel <kernel> --disk <disk> \
  --name cuda-compute-baseline --gpu-count 4

# 3. New lifecycle diagnostic, native-before/docker/native-after
python3 scripts/run-gpu-cuda.py <work> --kernel <kernel> --disk <disk> \
  --name cuda-lifecycle-0001 --gpu-count 4 --probe lifecycle

# 4. Standalone checker on any saved phase log
python3 scripts/gpu_lifecycle_checks.py <phase.stdout> \
  --expect-uuid-from-log <native-before.stdout>
```

Run serialized against the idle 4×A100 host under the existing
`kernel-build.lock` discipline. Acceptance for the lifecycle probe is the
checker verdict on all three phases (native-before, docker,
native-after) with identical UUID sets, plus the runner's
`diagnostic_complete`/clean shutdown evidence; `full_acceptance` remains
false and the separate stock Docker `--gpus` final suite is still
required afterwards.
