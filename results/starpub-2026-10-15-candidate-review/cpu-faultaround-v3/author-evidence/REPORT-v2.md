# REPORT-v2 — Counters overhead fix, real semantic fixtures, exact rebuild

Round: `arkclaude/round17/anonymous-fault-performance/v2`
Predecessor: v1 is **frozen and untouched** (`REPORT-v1.md`,
`MANIFEST-v1.json`, patches `cd9c00…/22fc55…`, build6 kernel `9a253e98…`,
v1 test and work tree). Everything v2 lives under `v2/`.

Mode: source inspection + build + host-side unit/harness validation only.
**No guest VM, Docker, GPU or performance execution by this agent** — the
exploratory A/B is scheduled by the parent
(`cpu-faultaround-owner-v1`, unchanged existing `cpu-touch-probe`,
native-before/baseline/candidate-off/on4/off-after/native-after). No
performance threshold, no auto main merge.

---

## 1. Parent review inputs addressed

All four owner documents were read and applied:
`OWNER-FAULTAROUND-PREVIEW.md` (addressed in v1),
`OWNER-FAULTAROUND-FINAL-REVIEW.md`,
`OWNER-FAULTAROUND-TEST-REVIEW2.md`, and `OWNER-V2-SCOPE.md`.

### 1.1 Counter default-off overhead (v2 code fix, new 0030)

v1 shipped unconditional clock reads on the disabled path:
`process.c` always called `fs_now_ns()` twice and `tlb.c` always called
`local_clock()` twice; the map/unmap wrappers also replaced the original
operations unconditionally. v2 corrects all of that:

* **Gate before the clock.** New inline brackets
  `uml_fs_time_begin()`/`uml_fs_time_end()` in `faultstat.h` test
  `uml_faultstat_enabled` first; when off, begin returns 0 and end is a
  no-op — zero clock reads, zero atomics. The v1 wrappers around
  `clock_gettime`/`local_clock` at the call sites are deleted.
* **Original ops restored when off.** `__um_tlb_sync()` selects
  `fs_map/fs_unmap` only when enabled; the default branch assigns the
  original `map`/`unmap` function pointers directly (no wrapper frames).
* **One clock, host monotonic, both contexts.** `local_clock()` (guest
  sched/jiffies source; coarse on this baseline and unsuitable for
  per-flush ns) is gone from the feature entirely. All timestamps come
  from `uml_fs_clock_ns()`, which wraps the existing `os_nsecs()`
  (host `CLOCK_MONOTONIC`, `arch/um/os-Linux/time.c:103`) — the same
  clock already used from kernel-side `arch/um/kernel/time.c` and from
  os-Linux code, so kernel and host samples are comparable.
* **Honest interval label.** The `userspace()` bracket spans resume →
  next trap and therefore **includes guest user execution**, not pure
  fault handling. Counters were renamed `run`/`run_ns` with this written
  into the header, the `/proc/uml_faultstat` comment block and this
  report. `flush_ns` remains the narrow broker-flush interval.
* **Atomic snapshots.** `/proc/uml_faultstat` reads every counter with
  `__atomic_load_n(ACQUIRE)` (and the enabled flag the same way); the
  set is documented as approximate across vCPUs.
* **No observer errno propagation.** Helpers return void; `fs_map`/
  `fs_unmap` invoke the op first and return its result byte for byte.
  Nothing in the instrumentation participates in control flow.
* The shared header includes **no headers at all** (kernel builds are
  `-nostdinc`; the v2 build proved `<stdint.h>` fatal in a kernel object
  and the final header uses only built-in integer types).

The disabled-path contract is proven two ways:
functionally (`tests/faultstat_unit.c`: 1,000,000 inc/add + begin/end
iterations with the enable flag at 0 → mock clock call count 0 and all
counters unchanged; enabled-mode deltas; non-monotonic sample discarded;
concurrent-writer snapshot monotonicity; wrapper preserves `-EIO…`) and
structurally (`tests/run_faultstat_checks.py`: no `local_clock`, no added
`clock_gettime`, gate precedes clock in begin/end, original ops wired in
the off branch, wrappers return `ret`, ≥12 atomic snapshot reads).

### 1.2 Semantic test rewrite (`faultaround_semantics_v2.c`)

The v1 test is not used by v2. The new binary emits exactly one JSON
object per line per case (`pass`/`fail`/`skip` are distinct), plus a
summary that must match the per-case tally; `--selfcheck` deliberately
emits a skip and a fail and exits 3. Every resource error path is
bounded and tracked:

* mmap/pipe failures become fail/skip records without touching
  uninitialized fds; failed `fork()` is reported and never followed by
  `waitpid(-1)`; pthreads are joined only when actually created.
* **PROT_READ and PROT_NONE run in separate children** — v1's single
  handler `_exit(42)` meant the PROT_NONE access was unreachable. The
  NONE child checks both read and write.
* munmap-mid-window runs in a dedicated trapping child while the parent
  independently proves neighbour pages survive.
* **userfaultfd uses the real `struct uffd_msg`/`UFFD_EVENT_PAGEFAULT`**
  (address and WRITE flag validated), bounded by design: a
  `CLONE_VM|SIGCHLD` faulter (same mm — uffd does not survive a
  COW-fork) and a resolver child; supervisor `poll()` 10 s with SIGKILL
  reaping of both children — a deadlock/hang is a fail, never a pass.
  Post-resolution content is the **written byte 0x11**, not zero.
  Negative fixtures are explicit: bogus API → EINVAL; idle NONBLOCK read
  → EAGAIN; resolver poll timeout path in a child with an untouched
  registered range (exit 2).
* **memcg requires OOM-kill evidence**: unique cgroup, bounded child,
  pass only on `WIFSIGNALED`+SIGKILL **and** a positive
  `memory.events:oom_kill` delta; `memory.peak` recorded; a plain
  non-signal ENOMEM exit is a fail ("allocation failure is not OOM-kill
  proof"); setup failures skip; removal of the cgroup is separately
  asserted (no stale-state success).
* Sparse residency is measured with `mincore()` (resident page count
  reported, including a `speculative_residency` boolean) instead of
  claiming unchanged charging; the owner off/on matrix supplies the
  policy comparison.
* Dense, sparse one-page-per-window fault bounds, read-zero → private
  write conversion, fork/COW parent isolation and 4-thread interleaved
  first touch remain, each as a separate per-case record.

`tests/run_semantics.py` compiles nothing itself; it enforces the
protocol: every line must parse, per-case names must be unique, the
summary must equal the tally, and the `--selfcheck` fixture must produce
the deliberate skip/fail and rc 3. It was itself negatively validated by
feeding it a malformed emitter (overall result fail, bad lines caught).
A host run is recorded with `--label host` and validates the harness
only; kernel evidence requires the owner's `--label guest` execution.

### 1.3 Exact config, full build, input hashes

* Fresh independent plain `cp -a` of the held authoritative tree into
  `v2/work/linux` (not a copy of any v1/shared tree); all 33 files of
  the owner manifest match the tested sha256 set exactly.
* **Exact tested config** (CPUSETS/virtiofs/IPVS and everything else)
  copied byte-for-byte; `olddefconfig` output is asserted
  byte-identical before building.
* Build: global exclusive `kernel-build.lock` acquired **per command**
  and released between chunks (170 s command bound, 180 s lock wait,
  `taskset` 0-3, `-j4`), full objects + vmlinux link. The complete,
  honest attempt history is listed below.
* 0029 is **byte-identical to frozen v1** (sha `cd9c00f5…`); the v2
  series is the same 0029 plus the corrected 0030, and both reapply
  cleanly (`--fuzz=0`) from pristine in order.

Hashes are bound in `MANIFEST-v2.json`: patches, test sources, the
33-file baseline match, the six post-apply source files, the config and
the linked kernel (`work/build/linux`,
`540c903a0da8fffe6424adafa1ae4e8510470763a5e0693e4d6643aece47c4b2`).
`build1.log` honestly records the first attempt's real `-nostdinc`
`<stdint.h>` failure that drove the header fix; `build2.log` and
`build-retry-1.log` record EX-lock queueing behind the parent GPU
lifecycle build; `build-retry-2.log` is the final successful build
(chunk 1 rc=137 at the 170 s bound with the lock released, chunk 2
rc=0 `LINK linux`, `CONFIG_BYTE_IDENTICAL`).

## 2. What v2 does NOT change

* The 0029 faultaround mechanism and its default-off/exploratory status;
  its OOM/residency/mincore accounting caveats documented in v1 stand.
* No new performance claim: v2 changes no mapping batching. Predicted
  gains remain owner-to-measure under the scheduled A/B.
* No guest/GPU/perf execution here; warm-store residual and runner
  routing (v1 H2/H3) remain future work.
