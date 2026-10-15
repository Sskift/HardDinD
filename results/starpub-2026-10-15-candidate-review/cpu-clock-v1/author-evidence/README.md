# Clock candidate v5 — correct frozen-config full-link build

Supersedes the **build-only/rejected** v4 (preserved read-only in
`../../rejected-clock-v4/kernel-candidate-v4-buildonly-rejected`). v4 linked
kernels from the stale generic `kernel/runtime.config`, which disabled
CONFIG_CPUSETS, VIRTIO_UML/FUSE/VIRTIO_FS, IP_VS, NF_CT_NETLINK — not the
pinned guest. v5 builds against the exact
`cpu-cpuset-owner-v1/kernel.config` with **zero SMP config drift**.

The two source patches are byte-identical to v4 (the v4 source fixes
stand); the difference here is build/config correctness and the chunk-lock
methodology.

## Baseline and source binding (verified, no make needed)

- Baseline: immutable tarball `2feaa704…` + frozen 23-patch series;
  `verify_frozen_33()` hashes all **33** frozen-patched files in
  `/root/harddind-rebuild/linux-7.2.10` → 33/33 OK.
- Candidate source `clock-v5/src` is an independent plain `cp -a` (no
  hardlinks; inode check). Of the 33 frozen files, exactly the five allowed
  files differ: `arch/um/{include/shared/os.h, kernel/Makefile,
  kernel/time.c, os-Linux/skas/process.c, os-Linux/start_up.c}`; the other
  28 are byte-identical. New file `arch/um/kernel/stall_diag.c`; generic
  `kernel/sched/fair.c` = pristine tarball
  (`fair.c.pristine`, sha256 `0002fb0d…`) + exactly 3
  `harddind_stall_cfs` hook references.

## Config

- SMP: `cpu-cpuset-owner-v1/kernel.config` copied verbatim, `olddefconfig`,
  then semantic equality over all 617 CONFIG_ symbols asserted BEFORE and
  AFTER build → **drift 0** (CPUSETS/VIRTIO_UML/FUSE/IP_VS/NF_CT all
  present, confirmed).
- TT (!SMP, UML_TIME_TRAVEL_SUPPORT=y): only the frozen config plus the two
  flips and unavoidable kconfig deps; the full semantic diff is printed and
  saved, not silently merged from runtime.config.

## Chunk lock methodology (OWNER-BUILD-CHUNK-LOCK-V5)

`build-chunk.sh <cfg|link|audit> [smp|tt]` performs **one** bounded action:
EX is acquired fresh via `flock -n` with a bounded 60 s wait (exit 9 =
DEFER, no build), one `taskset -c 0-3 timeout 175 make` runs, and the lock
is released when the process exits. Exact make rc recorded: 0 linked,
124 chunk timeout (resume later; incremental objects kept), any other
nonzero is a hard compile/link failure that is never retried. The driver
`build-v5.sh` sleeps 6 s between acquisitions to yield the slot and exits
DEFER while an owner experiment is running. No unbounded flock, no loop that
holds the lock.

Run (only when the serialized slot is free; safe to re-run, resume-safe):

```
bash analysis/kernel-candidate-v5/build-v5.sh
```

or one chunk manually:

```
bash analysis/kernel-candidate-v5/build-chunk.sh link smp   # one 175s chunk
```

## Audit

`build-chunk.sh audit` verifies: linked config drift (SMP 0), one
`harddind_stall_diag_ms`, one `harddind_hr_sched_clock`, one `sched_clock`,
no undefined `harddind*`, and that the symbol-binding case matches the
shipped pinned kernel's own globals (`seccomp_threads`/`jiffies` are
lowercase in the final UML link — final-link localization, not a missing
definition; nm evidence recorded). Kernel sha256/size written to
`clock-v5/build-state.json`.

## Scope (unchanged wording)

Configured quota/period/weight values unchanged; scheduler-clock
granularity and guest scheduling/accounting behavior explicitly changed in
HR mode; WALL time, no steal compensation; overcharge is hypothesis until
the owner control/HR A/B. Parameters: `harddind_stall_diag=<ms>` (default 0),
`harddind_hr_sched_clock=<0|1>` (default 0).
