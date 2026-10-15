# REPORT-v1 — Anonymous first-fault performance under HardDinD UML

Round: `arkclaude/round17/anonymous-fault-performance`
Mode: **source inspection / build only**. No VM, GPU, or native benchmark was
run by this round; all performance numbers below are owner-measured
(`cpu-touch-owner-v1`, `gpu-phase-owner-v2`) or source-derived predictions
clearly marked as such. The exact tested kernel was not modified; all work
was done in an independent byte-copy of the authoritative tree.

---

## 1. What the data says

Owner control experiment `cpu-touch-owner-v1` (fresh `MAP_ANONYMOUS`
1/4/16 MiB, one store per 4 KiB page, rusage fault deltas outside the timed
loop; 3 native processes before, 3 nested Docker, 3 native after; 8 reps per
size per process). Medians:

| Size | Native cold | Guest cold | Guest/native | Native warm | Guest warm | Guest minflt |
|---|---|---|---|---|---|---|
| 1 MiB | 0.413 ms (1.61 µs/page) | **5.675 ms (22.17 µs/page)** | **13.7x** | 2.0 µs (7.7 ns/page) | 11.3 µs (44 ns/page) | 256 |
| 4 MiB | 2.041 ms (1.99 µs/page) | **22.272 ms (21.75 µs/page)** | **10.9x** | 8.3 µs (8.1 ns/page) | 28.4 µs (27.8 ns/page) | 1024 |
| 16 MiB | 9.000 ms (2.20 µs/page) | **82.979 ms (20.26 µs/page)** | **9.2x** | 68 µs (16.7 ns/page) | 92 µs (22.6 ns/page) | 4096 |

Observations that drive the analysis:

1. Guest cost is **~20 µs per minor fault, constant from 256 to 4096
   pages** — a fixed per-page cost, not a bandwidth or zeroing cost.
2. Both sides count exactly one minor fault per page, zero major faults.
   Nothing is prefaulted in the baseline; the gap is fault *handling*.
3. Warm (resident) pages are 8–44 ns/page: once host PTEs exist there is
   no UML involvement at all. The remaining ~3x warm gap is a separate,
   non-fault residual (see §6, not addressed by the patches).
4. GPU owner result (`gpu-phase-owner-v2`, same binary): cold 4 MiB output
   first DtoH 3.7–4.4 ms native vs 13.4–16.7 ms Docker; prewritten ~1 ms;
   cost follows the first fault on the *used* GPU, and prefault moves the
   same work outside the timed region. **The CPU control proves anonymous
   first-fault handling alone costs ~20 µs/page; it does not prove that all
   GPU copy latency is fault cost** — GPU fault counts are still missing.

---

## 2. Exact fault cycle (boot: `seccomp=on seccomp_threads=1
seccomp_broker_cache=1 seccomp_spin=256 ncpus=4`, 7.2.10 + harddind 33
files + 0028, kernel sha256
`b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502`)

The guest does **not** use `/proc/mm` (absent on this host). It is the
seccomp/SKGAS variant (`arch/um/os-Linux/skas/{process,mem}.c`,
`arch/um/kernel/skas/{process,mmu,stub,stub_exe}.c`):

* one **broker** host process per guest mm (`mm->context.id`) that executes
  host mmap/munmap stubs;
* one **runner** host process per vCPU (`mm->context.cpu_ids[cpu]`), cloned
  `CLONE_VM` from the broker (`stub_clone`, stub.c:63) so runner and broker
  share one host `mm_struct`;
* guest user code runs natively on the runner under a seccomp filter; a
  missing host VMA raises `SIGSEGV` in the runner's stub handler
  (`stub_signal_interrupt`, `arch/um/kernel/skas/stub.c:410`).

Steady-state per 4 KiB anonymous write fault:

1. **Runner trap.** Store → no host VMA → host `SIGSEGV` delivery, SA_SIGINFO
   frame on the altstack. Handler saves si/mcontext offsets into the shared
   `stub_data` and exchanges the futex word `FUTEX_IN_CHILD → IN_KERN` with
   a `FUTEX_WAKE` (`stub-futex.h`, stub.c:442); after the bounded 256-pause
   spin it sleeps in `FUTEX_WAIT`.
2. **vCPU wake + fault decode.** `userspace()`
   (`arch/um/os-Linux/skas/process.c:600`) was parked in
   `wait_stub_done_seccomp()` (process.c:150); it wakes, copies fault info
   out of the shared mcontext and calls `segv_handler → segv →
   handle_page_fault` (`arch/um/kernel/trap.c:140`).
3. **Guest VM fault.** mmap read lock → `handle_mm_fault →
   do_anonymous_page` (`mm/memory.c:5287`): `folio_prealloc`
   (`vma_alloc_zeroed_movable_folio` → GFP zeroed alloc) — the first touch
   of the UML direct-mapped memfd page is **host fault #A (zero fill) in
   the vCPU process** — memcg charge, rmap, LRU, then `set_ptes`
   (`arch/um/include/asm/pgtable.h:233`) marks each PTE `_PAGE_NEEDSYNC`
   and marks one 4 KiB dirty range under `sync_tlb_lock`.
4. **Broker flush (separate round trip).** Next `userspace()` iteration
   calls `current_mm_sync()` → `um_tlb_sync` → `__um_tlb_sync`
   (`arch/um/kernel/tlb.c:170`): walks the single dirty PTE, calls
   `phys_mapping()` (offset == guest PFN in the single RAM memfd) and
   queues one `STUB_SYSCALL_MMAP` (`map()`, mem.c:242) into the **broker**
   queue; `syscall_stub_flush()` does a full kernel→broker→kernel rendezvous
   (`do_syscall_stub`, mem.c:94; `wait_stub_done_seccomp`): the broker
   executes `mmap(addr,4K,PROT_RW,MAP_SHARED|MAP_FIXED,physmem_fd,pfn)` and
   hands back.
5. **Runner resume round trip.** `set_stub_state()` writes registers into
   shared memory, then `wait_stub_done_seccomp(runner, running=0)` hands the
   (empty) runner batch and wakes the runner; the runner `rt_sigreturn`s,
   restarts the store and takes **host fault #B — a shmem minor fault that
   finds the already-instantiated page** — then hits the next page, faults
   again (`SIGSEGV`), and reports back to the vCPU.

Per page, therefore: **two futex rendezvous** (broker flush + runner
resume/trap; four `FUTEX_WAKE`/scheduling events), one host `SIGSEGV` frame
build + `rt_sigreturn`, one host `mmap(MAP_FIXED)`, **two** host page
faults (zero-fill in the vCPU direct map + shmem remap in the runner), plus
the ordinary Linux VMA/allocator work and the UML bookkeeping. Every one of
these is serialized through per-page trap entry; the 256-pause spin hides
only part of the scheduling latency.

The deferred-sync design is already optimized for batching:
`set_ptes` only marks a coalescing range (`um_tlb_mark_sync`),
`__um_tlb_sync` drains it once, and `map()` compresses adjacent records with
contiguous memfd offsets (mem.c:186-261). Patches 0001 (bounded spin), 0004
(parallel waiter), 0006 (skip idle sync) and 0007 (cached memfd) already
removed everything that can be amortized **within** a single page. What is
left is the page count itself.

---

## 3. Ranked hypotheses/evidence

### H1 — The fixed rendezvous is paid once per 4 KiB page; collapse several
real faults into one rendezvous. **Primary finding; patch 0029 implements
it.** (source-confirmed, performance gain is owner-to-measure)

Nothing in the architecture requires one signal per *page* — it is one
signal per *host VMA miss*. Mainline solves the equivalent problem with
filemap faultaround and anonymous mTHP; Linux 7.2.10 already contains
anonymous PTE-mapped large folios (`alloc_anon_folio`/`map_anon_folio_pte_*`,
mm/memory.c:5161-5279), but they are gated on
`CONFIG_TRANSPARENT_HUGEPAGE` (mm/Kconfig:848), which requires
`HAVE_ARCH_TRANSPARENT_HUGEPAGE`; UML does not select it, the guest config
has THP and COMPACTION off, and enabling PMD THP would require real
huge-PMD support in UML's shadow table walk (`update_pmd_range`,
tlb.c:95 treats a present PMD as a PTE page). Reusing that machinery on UML
is a much larger, riskier change.

**Patch 0029 (`0029-um-anonymous-faultaround.patch`, opt-in
`uml_faultaround=<order>`, default off):** after a successful *user write*
fault, resolve the other empty PTEs of an aligned 2^order-page window
(order ≤ 5 ⇒ ≤ 32 pages/128 KiB), **by calling the ordinary
`handle_mm_fault(…|FAULT_FLAG_USER|FAULT_FLAG_WRITE…)` for each neighbour**.
Gating mirrors filemap faultaround/THP containment rules:

* plain private anonymous VMA only (`VM_WRITE`, not `VM_SHARED`, no
  `vm_ops`; rejects PFNMAP/MIXEDMAP/IO/HUGETLB/GROWSDOWN/UFFD_MISSING/UFFD_WP);
* whole window aligned and fully inside the VMA and one PMD;
* only `pte_none` slots; existing/zero-page/racing PTEs skipped;
* stop at the first OOM/SIGBUS/SIGSEGV/hwpoison/RETRY and at fatal signals;
  a neighbour fault never delivers a signal.

**Functional semantics are inherited from the unchanged fault path, not
reimplemented; but residency/accounting *timing* is deliberately different
(see warning below).**

* **zero-fill**: neighbours allocate the same GFP-zeroed anon folios;
* **COW/fork**: real guest PTEs and real anon rmaps exist before the runner
  resumes, so fork COWs them exactly as touched pages (`copy_pte_range`),
  and a later first write COWs normally. Zero-page read mappings and RO/
  migration/swap entries are PTE-present and are *not* pulled into a window;
* **mprotect/munmap/unmap**: neighbours are ordinary NEEDSYNC PTEs; the
  existing range mark + `__um_tlb_sync` walk queues the correct map/unmap
  records (tlb.c:50-219), and later protection changes or unmaps invalidate
  them through the same broker path;
* **resource accounting**: every neighbour adds `MM_ANONPAGES`, rmap/LRU
  and memcg charge inside `do_anonymous_page`;
* **thread isolation**: host mappings still change only via the serialized
  broker and the shared host mm; no per-runner state is added; concurrent
  vCPUs merely contribute more NEEDSYNC PTEs to the same locked range walk.
  If the 67-deep stub queue fills (`(4096-320)/sizeof(stub_syscall) = 67`),
  the existing `syscall_stub_alloc()` mid-walk auto-flush (mem.c:165-184)
  bounds it;
* **bounded cost/queue**: ≤ 33 map records per window plus the one cached
  MEMFD record, one PMD, no new page-table levels, no new allocator flags.

> **Timing/observable semantics are NOT identical (parent review v1
> incorporated).** Calling `handle_mm_fault` does not make speculative
> faults free of behavioral change: up to 2^order pages become resident,
> `mincore()`-visible, RSS/memcg-charged and counted as minor faults at the
> *first* touch of the window; under a tight memcg/RLIMIT/host-memory limit
> the process can receive the OOM/SIGKILL up to a window earlier, and a
> workload that only touches one page per window does allocate the others.
> That is the THP-always trade, accepted only because the feature is
> boot-opt-in and default-off; it must be measured under memory limits, not
> described as "unchanged".

**Lock/flag contract (parent review v1 incorporated).** `FAULT_FLAG_DEFAULT`
in this kernel is `ALLOW_RETRY|KILLABLE|INTERRUPTIBLE` (linux/mm.h:684); a
neighbour fault could return `VM_FAULT_RETRY`/`COMPLETED` having *released
the mmap read lock* that `handle_page_fault()` still intends to unlock.
The patch therefore:

* invokes neighbours with expressly
  `FAULT_FLAG_USER|FAULT_FLAG_WRITE` (no retry/killable/interruptible);
* excludes every userfaultfd VMA (`VM_UFFD_MISSING|WP|MINOR`), stack,
  device/special/hugetlb VMAs and any VMA with `vm_ops`;
* performs the emptiness probe under the PTE lock
  (`pte_offset_map_lock`/`pte_unmap_unlock`), relying on the mmap read lock
  for table stability and on the guest config having no THP PMD collapse;
  the window is contained in the faulting PMD;
* treats `VM_FAULT_RETRY|COMPLETED` as an impossible contract violation
  (`BUG()`, fail-stop rather than return with an unbalanced lock);
  OOM/SIGBUS/hwpoison end the window and the page is faulted on real access.

Mechanics of the speed-up: one window costs **one** broker flush and **one**
runner trap regardless of size. The UML direct-map zero fills of all pages
happen during the single vCPU rendezvous; the queued mmap records for
contiguous guest PFNs compress (buddy order-0 allocations within an aligned
window are usually contiguous) into one host `mmap`; subsequent stores in
the window never leave the runner — the host serves them as plain shmem
minor faults against the existing VMA.

Source-based prediction, not a claim: at order 4 (16 pages) a 4 MiB stream
becomes 64 rendezvous instead of 1024; plausible cold median moves from
~22 ms toward the low-single-digit-ms range (64 × ~15–25 µs fixed + ~1024 ×
~0.5–1 µs aggregate host fills). Owner matrix must measure; no threshold is
accepted here.

**Response to parent early review (`OWNER-FAULTAROUND-PREVIEW.md`):**

| Review point | Resolution in 0029 |
|---|---|
| `FAULT_FLAG_DEFAULT` carries ALLOW_RETRY; RETRY/COMPLETED can drop the mmap lock the caller unlocks | Neighbours now use expressly `FAULT_FLAG_USER|FAULT_FLAG_WRITE` (no retry/killable/interruptible); unexpected RETRY/COMPLETED `BUG()`s instead of returning with an unbalanced lock |
| Restore the original fault state contract | mmap read lock provably stays held; VMA revalidated by containment; helper only runs after the primary fault succeeded |
| Audit walk synchronization / invalid PMD / holes | Emptiness probe moved under the PTE lock; mmap read lock pins the tables; window confined to the faulting PMD (PTE page already exists); pmd-present check retained; relies on guest THP being compiled out (documented) |
| Do not claim OOM/accounting unchanged | Patch help text and §H1 state explicitly: residency/mincore/RSS/memcg/minflt move earlier and OOM/SIGKILL can arrive up to a window sooner; default-off exploratory aggregation |
| Required tests | Added sparse one-page/window, dense, read-zero→write COW, fork/COW, mprotect RO+NONE, munmap mid-window, pthreads, live userfaultfd exclusion (skip if denied), and a tight memcg-limit forked child (skip if no cgroup access; owner docker matrix covers limits) |
| One sequential gain insufficient | Feature stays opt-in; §5 matrix demands order sweep, off-equals-baseline, spin/cpu variants, full 384-check docker suite, COW/mprotect/limits and GPU non-conflation |

### H2 — The broker flush is a second rendezvous on the fault critical path.
**Future patch, designed but not shipped: ordering hazard identified.**

In parallel mode the fault pays a broker round trip *and* a runner round
trip, even though the parked runner shares the broker's host mm and its
seccomp filter already allows mmap. The map batch for the faulting PTEs
could be executed by the waking runner itself (as in non-parallel mode),
removing one peer schedule per fault/window. Straight naïve routing is
**unsafe**: a concurrent munmap/mprotect on another vCPU flushes *unmap*
records through the broker, and an unordered stale runner `mmap` could
recreate a host VMA after the broker unmapped it (orphan host mapping). A
correct version needs a generation/drain protocol that orders deferred
runner batches against broker invalidations (or restriction to windows
quiesced under the existing locks). It is worth doing after H1 is measured,
because H1 already cuts rendezvous count ~16x; expected additional benefit
is the broker schedule slice only.

### H3 — Two host faults per guest page (zero-fill + shmem remap).
**Bounded micro-opt candidate, not shipped.**

The vCPU zero-fills the memfd page through the kernel direct map (host
fault A); the runner then remaps the existing page on retry (host fault B).
B is not avoidable externally without remapping foreign mms (forbidden).
For a faultaround *window* the single compressed broker `mmap` could add
`MAP_POPULATE`, instantiating all runner-side PTEs inside the one syscall
(pages already exist in shmem) instead of per-store minor faults; the same
total host fault work is batched under one mmap lock and the restart path
is skipped. Requires a per-record populate flag and care not to trigger
read-ahead for non-instantiated read-only/zero-page slots.

### H4 — Guest kernel-side options (mTHP sysfs / Kconfig) are the
configuration twin of H1; documented, not enabled.

If UML ever selects `HAVE_ARCH_TRANSPARENT_HUGEPAGE` with PMD orders
suppressed, enabling e.g. 64 KiB anonymous mTHP plus the per-size sysfs
knobs would give the H1 effect via upstream code (and upstream
memcg/OOM/madvise policy). 0029 deliberately does not require that
infrastructure and keeps PMD THP out of scope.

### H5 — Warm-store residual (~8–44 ns/page, ~3x) is NOT a fault cost.

Warm loops incur zero faults and zero UML traps; likely contributors are
shared-mmap dirty accounting vs private-anonymous pages, placement/cache
and vCPU scheduling. Do not "fix" via pre-touching; owner should profile
(e.g. host perf counters on the runner) separately. It is reported as a
boundary, not optimized in this round.

---

## 4. Deliverables in this round

```
patches/0029-um-anonymous-faultaround.patch      (feature, default off)
patches/0030-um-fault-rendezvous-counters.patch  (counters, default off)
tests/faultaround_semantics.c                    (owner-run guest test)
work/                                            (independent tree, build)
MANIFEST-v1.json                                 (hashes + build facts)
```
Patch/test hashes:
`0029` = `cd9c00f5596d82177d8353c218b07c6db87315380a03a6cab6097150dd495a4f`,
`0030` = `22fc5559c3573679177864c586015b437e9ee32904a13020d5a6e4b068ebdd53`,
test   = `1329033dca4342188dd43635007dc8f64765ef9feecd51d2c2ffb12c1d1fef01`.

**0030 (`uml_faultstat=1`, `/proc/uml_faultstat`)** — minimal bounded
machine-wide counters: userspace SIGSEGV reports, successful user write
faults, faultaround windows/pages/gated-skips, TLB range syncs, queued
map/unmap records, broker flush count and cumulative ns, runner resume
count and cumulative ns. Default-off path is one predicted-not-taken
branch; the shared header uses only compiler atomics so the same hooks work
in os-Linux compilation. This gives the owner the per-fault attribution
(handoff count vs mmap flush ns vs runner wait ns) that H2/H3 need, without
any VM changes when off.

### Build verification (this round, source-only)

* Independent plain `cp -a` of the authoritative read-only tree (ext4, no
  reflink support; no hardlinks); all 33 tracked files match the
  `gpu-completion-owner-v1` post-0028 sha256 manifest exactly.
* Both patches apply with `patch --batch --forward --fuzz=0 -p1` and
  reproduce the edited files byte-identically.
* Object **and full vmlinux link** with the exact tested config:
  `make O=work/build ARCH=um olddefconfig && make O=work/build ARCH=um
  -j4`, CPUs 0-3, under the global exclusive `kernel-build.lock`
  (`flock -w 180`, per-command 170 s timeout). Result **BUILD_OK**
  (`work/build6.run.log`, after the parent-preview rework); built kernel
  `work/build/linux` sha256
  `9a253e98116c97a68f829a3a2474700d0e99ecfe7fd28085145192c37f3699fb`.
  The build was rerun after fixing (1) a missing shared `<init.h>`
  include, (2) a missing semicolon on `__uml_setup`, (3) the wrapper
  script's handling of `timeout`'s SIGKILL exit code, and rerun for the
  parent-review rework (explicit no-retry flags/PTL/UFFD-minor); logs
  `work/build{,2,3,4,5,6}.run.log` record all attempts honestly.
* `trap.o`, `tlb.o`, `faultstat.o`, os-Linux `skas/process.o` compile
  warning-free under `-Werror`; both new boot parameters and the
  `/proc/uml_faultstat` initcall are present in the linked binary;
  `.config` is byte-identical to the tested `kernel.config`.
* `tests/faultaround_semantics.c` compiles `-Wall -Wextra` clean and its
  logic passes natively as a host unit sanity check; it is written for the
  owner to execute **inside the guest**, where it matters.

---

## 5. Owner-run regression/performance matrix

All runs use the unchanged launch path; the only differences are the two
new optional kernel parameters, appended to the kernel command line in
`repo/scripts/run-docker.py`'s `cmd` list (they are kernel arguments, not
script flags — e.g. `"mem=4G", "ncpus=4", ..., "uml_faultaround=4",
"uml_faultstat=1"`). Patches apply on the frozen baseline with
`patch --batch --forward --fuzz=0 -p1` (verified clean; 0030 applies after
0029). Keep prefault excluded from timed regions; report full process cost
including the faultaround burst (it is real work).

### A. Performance (run `cpu_touch_probe`, sizes 1/4/16 MiB, 8 reps × 3
docker + native controls), kernel matrix:

1. baseline (`b8a58b…`)
2. 0029+0030, `uml_faultaround=0` (must equal baseline within noise)
3. `uml_faultaround=1` (8 KiB window)
4. `uml_faultaround=2` (16 KiB)
5. `uml_faultaround=3` (32 KiB)
6. `uml_faultaround=4` (64 KiB — recommended)
7. `uml_faultaround=5` (128 KiB)
with `uml_faultstat=1` on one run: record `/proc/uml_faultstat` before/after
each size (segv, window, window_page, map, flush, flush_ns, resume,
resume_ns). Report cold/warm median, minflt (expect bursts up to window
size), and total wall including faultaround.

### B. Semantics (in guest, `uml_faultaround=4` and =0 must both pass):

* `tests/faultaround_semantics.c` (compiles -Wall -Wextra clean; host
  sanity PASS): dense zero-fill + rusage bounds; **sparse one-page-per-
  128KiB-window** touches with untouched pages reading zero; read-zero
  page then sparse write (private COW conversion); fork/COW parent
  isolation; mprotect RO **and PROT_NONE** mid-window write-block and
  restore; munmap mid-window really unmaps while neighbours survive;
  4-thread interleaved first touch; **userfaultfd exclusion** live check
  (UFFD missing fault must be delivered/resolved, skipped when the
  environment denies uffd); **tight 8 MiB memcg limit** enforcement in a
  forked child (skipped when the harness cannot create cgroups — the
  owner docker matrix supplies that environment).
* Existing docker suite (384 GPU/feature checks) unchanged; specifically
  re-run mapping/mprotect/thread/share/posix suites and the
  `HARDDIND_MAPPING_SHA256`/thread/mapping semantic fingerprints.
* Explicit edge cases to add if time permits: VMA smaller than window;
  16 MiB stream (4096 pages); guarded stack near rlimit; mlock region;
  process under a tight memory cgroup/RLIMIT_AS (observe OOM timing:
  window-charged with feature on, page-at-a-time with off — both must kill
  the right workload, never corrupt); madvise DONTNEED/FREE after first
  touch; mremap of a populated window; multi-process (not only multi-thread)
  COW; nested fork while a window is warm.

### C. Scheduler/concurrency: same matrix with `seccomp_spin=0` and 256;
`ncpus=1` and `ncpus=4`; broker cache on/off (parameter must not interact).

### D. GPU boundary: do **not** conflate with this work — rerun
`gpu-phase-owner-v2` cold/prewritten D/H copies unchanged; if
faultaround changes GPU first-copy numbers, report faultstat fault counts
alongside before attributing anything (GPU fault counts remain the
missing data the GPU-side report needs).

Acceptance for the feature: zero semantic-suite/full-docker regressions at
order 0; semantic pass at order 4; performance numbers reported raw with no
pre-accepted threshold.

---

## 6. Boundaries / non-goals

* No pre-touching outside measured work, no pre-population of guest
  memory, no changes that map guest memory without real guest PTEs.
* No host permission, cgroup-write, KVM, socket, or security-disabling use;
  the patches use only the existing broker/stub mapping path.
* Held kernel/source/output untouched; no git push; parent integrates.
* No VM/GPU/native benchmarking by this round; performance claims are
  labelled predictions pending the owner matrix.
* H2 (runner-routed maps) and H3 (MAP_POPULATE window) deliberately left
  unimplemented pending the measured size of H1 and the faultstat
  attribution; both are sketched with their correctness hazards.
* The warm-store residual (H5) is out of scope for fault optimization.
* The OOM-timing change of faultaround is intentional, documented, and
  gated behind an explicit boot parameter (THP precedent); order 0 keeps
  byte-for-byte baseline behavior.
