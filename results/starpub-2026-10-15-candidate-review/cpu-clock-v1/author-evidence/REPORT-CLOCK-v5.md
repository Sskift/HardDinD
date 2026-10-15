# REPORT-CLOCK-v5 — frozen-config full-link candidate (source/config bound, chunk-locked)

Response to OWNER-CLOCK-V4-CONFIG-P0.md and OWNER-BUILD-CHUNK-LOCK-V5.md.
v4 is preserved read-only in
`rejected-clock-v4/kernel-candidate-v4-buildonly-rejected/` and is labeled
**build-only/rejected**: it linked from the stale `kernel/runtime.config`
and dropped CPUSETS / VIRTIO_UML / FUSE / VIRTIO_FS / IP_VS / NF_CT_NETLINK.
No source/config of v4 is used as build evidence.

All work is source/script/configuration only; **no guest/GPU/perf run**. The
full SMP link from the exact frozen config is built strictly through the
one-chunk-per-lock driver and is recorded as PENDING while owner experiments
(`gpu-lock-repair-owner-v1` at time of writing) hold the serialized slot.

## 1. Corrections vs the two owner notes

**Config (P0).** v5 copies `/root/harddind-rebuild/cpu-cpuset-owner-v1/kernel.config`
(sha256 `bd17febf…`) verbatim as `.config`, runs only `olddefconfig`, and
asserts semantic equality of every CONFIG_ symbol before and after build:
**1461/1461 symbols, drift 0** (verified on the seeded obj-smp config).
CPUSETS=y, VIRTIO_UML=y, FUSE_FS=y, VIRTIO_FS=y, IP_VS=y, NF_CT_NETLINK=y
present. The stale runtime.config is never touched. TT config is the frozen
config with only `-d SMP -e UML_TIME_TRAVEL_SUPPORT`; its full unavoidable
dependency diff is printed/audited rather than merged silently.

**Lock.** The interrupted PGID held EX across a 12×175 s loop. v5 replaces
that: `build-chunk.sh` acquires EX **per invocation** with `flock -n` and a
bounded 60 s wait (exit 9 DEFER), runs one `taskset -c 0-3 timeout 175
make`, records the **real make rc** (0/124/hard-fail; no `if make` masking),
and releases on process exit. The driver re-acquires only after a 6 s gap
and DEFERS when an owner experiment is running. Verified live: with the
owner holding EX, one chunk attempt waited bounded and exited 9 with no
build started.

**Source 33 binding.** Actual hash verification (not grep idempotence): of
the 33 frozen-patched files, 28 are byte-identical and exactly the five
allowed files differ (os.h, kernel/Makefile, time.c, skas/process.c,
start_up.c). New `stall_diag.c`; generic `kernel/sched/fair.c` binds to the
pinned pristine tarball file (`fair.c.pristine` sha256 `0002fb0d…`) plus
exactly three `harddind_stall_cfs` hooks.

**Symbol case.** Both flags are non-static globals with exactly one
definition; `nm` of the final UML executable shows lowercase `b` — the same
final-link localization the **shipped pinned kernel** shows for its own
equivalent globals (`jiffies`→`d`, `seccomp_threads`/`uml_seccomp_spin_limit`
→`b`, `sched_clock`→`t`, confirmed on `cpu-cpuset-kernel-v1`). The audit
compares the case against that pinned precedent and requires one
definition + zero undefined `harddind*`; it does not assume uppercase.

v4's actual code fixes are retained (one definition, standard
`__ratelimit`/`DEFINE_RATELIMIT_STATE`, single-printk records,
`last_futex_err` semantics, `time_init` comment, acquire/release immutable
anchor); the v5 patches are byte-identical in source content to v4's.

## 2. Verification performed this session (no heavy build)

- `verify_frozen_33`: 33/33 frozen files match.
- `verify_candidate_source_binding`: 28 identical, 5 expected changed, no
  unexpected, new file present, fair.c bound to pristine.
- Frozen config equality on the resume tree: 1461 symbols, drift 0.
- Chunk lock protocol: bounded non-blocking acquire returns 9 while busy;
  syntax checks on all three scripts.
- Independent source copy (plain cp -a; inode differs from reference; no
  hardlinks); the v5 tree currently holds 920 incremental objects from the
  interrupted session built under the exact frozen config.

## 3. Build results

**SMP — full-linked and audited** (one ~99 s chunk after the resumed
920-object warm tree; rc 0, lock released):

- kernel `clock-v5/obj-smp/linux` sha256
  **`5b3d3c507a00cd6c56dfb0dfec06f07b62633c4e9a487e5905ab960a105c10b0`**,
  151 013 048 bytes; linked config sha256 **`bd17febf…` — byte-identical to
  the frozen kernel.config** (1461 symbols, drift 0).
- audit: exactly one `harddind_stall_diag_ms`, one `harddind_hr_sched_clock`,
  one `sched_clock`, no undefined harddind symbols; binding case matches the
  shipped pinned kernel precedent; AUDIT_OK
  (`smp-symbol-binding.txt`, `smp-sched_clock-disasm.txt`).
- frozen features compiled in: cpuset.o, virtio_uml.o, fuse/virtio_fs.o,
  xt_ipvs.o, nf_conntrack_netlink.o all present
  (`frozen-feature-objects.txt`).

**TT — PENDING owner slot.** Config prepared (frozen + SMP off + TT on; 52
explicitly enumerated semantic diffs in `tt-config-diff.txt`, all SMP/UP
kconfig dependencies plus the two requested flips; e.g. CPUSETS disappears
because it depends on SMP, TINY_RCU/TINY_SRCU replace TREE_*, NR_CPUS=1).
First link chunk exited rc=124 with 1212 objects (lock released); the next
chunk attempt DEFERRED (exit 9) when an owner experiment took EX. Resume
when the slot is free:

```
bash analysis/kernel-candidate-v5/build-chunk.sh link tt   # one 175s chunk
bash analysis/kernel-candidate-v5/build-chunk.sh audit tt  # after it links
```

No claim of a TT full link is made until its kernel hash is in
build-state.json.

## 5. Scope

Configured quota/period/weights unchanged; HR mode explicitly changes
scheduler-clock granularity and guest scheduling/accounting behavior; WALL
time, no steal compensation; overcharge remains hypothesis until the owner
control/HR matrix. A test observation is not performance acceptance.

## 6. Deliverable hashes

```
05ce7d904d6e059733e2975e118856126f82e8031d1e3241fafc3dd5a8274ddd  0001-...stall-rendezvous-cfs-throttle-diag-v5.patch
27d37f5f5ec7c1b0dc7bcb12ce6d7d18bbcbabfbd08de089f57127f800a5d  0002-...hr-sched-clock-AB-v5.patch
b887770912314e060845932bf1811fff5c01288cf21ef3499defb7819df243ca  build-chunk.sh
a894419795b840c5b03f555c6798a76e3e2d1f7ad0fdde1306efcce4d9a89021  build-common.sh
5e4748175b947705b65668bbd6716243ca4707113629144b50d4766b32c9d5a8  build-v5.sh
de6196a86b5b343df13eb98753d929c909a8d174cace94137c56df516201d  README.md
9923c4b59333a8c1908c576157d213feaa5f1efaf7813ffb58cda21285a90f19  clock-v5/build-state.json (pre-build)
bd17febf02803c6b966948829fa25d3bfc85a354ac8746afaa3422a8ec65549b  frozen-cpuset-kernel.config
```
