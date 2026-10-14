# REPORT-CORRECTION — fp-owner-review-v4 independent review

Date: 2026-10-14. Supersedes **C1** and **M2** in `REPORT.md` (preserved
unchanged in this folder). Method: compiler-produced layout evidence from
the owner's object-file DWARF dumps (`owner-eager-layout-dwarf.txt`,
`owner-lazy-layout-dwarf.txt`, from `arch/um/kernel/um_arch.o`) plus read
of pristine linux-7.2.10 headers. No build, VM, ptrace or GPU run by me for
this correction. One read-only whole-vmlinux `readelf` scan of the lazy
kernel had completed before the owner's guidance (artifacts
`dwarf/lazy-layout.txt`, `dwarf/lazy-task.txt`); its numbers agree with the
object proof and are quoted below — no new whole-kernel scans.

## C1 RETRACTED — no slab overflow; compile-time sizeof(task_struct) grows

My error was moving the `fp[]` offset by +40 while holding
`sizeof(struct task_struct)` constant. It is a compile-time value and the
new descriptor fields are inside it.

Measured layout (DWARF, owner object dumps; my vmlinux scan matches lazy):

| | eager (no 0018) | lazy (0018) |
|---|---|---|
| `task_struct` byte_size (DW_AT_alignment 64) | **2688** | **2752** (+64) |
| `task_struct.thread` offset | 2200 | 2200 |
| `thread_struct` byte_size | 456 | 496 (+40) |
| `thread_struct.regs` offset | 200 | 200 |
| `pt_regs` / `uml_pt_regs` byte_size | 256 | 296 (+40) |
| `uml_pt_regs.fp` offset | 256 | 296 |
| **fp absolute offset in task_struct** | 2200+200+256 = **2656** | 2200+200+296 = **2696** |

Allocation (`arch/um/kernel/um_arch.c:395`, unchanged):
`arch_task_struct_size = sizeof(struct task_struct) + host_fp_size;`

- eager: object end = 2688 + H; XSTATE span = [2656, 2656 + H); **32 bytes
  spare**, no overrun.
- lazy: object end = 2752 + H; XSTATE span = [2696, 2696 + H); **56 bytes
  spare**, no overrun.

Growth accounting: the six fields shift `fp` by +40; `task_struct` is
64-aligned (DW_AT_alignment: 64), so its own trailing padding changes from
32 (2656→2688) to 56 (2696→2752); total compile-time growth is
40 + 24 = **64**, fully inside `sizeof`. The `+ host_fp_size` tail and the
slab cache (`kernel/fork.c:860-867`) therefore bound every full-frame copy
(mcontext entry/exit, signal, exec, ptrace regsets) with margin.

`arch_dup_task_struct` copies `arch_task_struct_size` bytes
(`arch/um/kernel/process.c:215`): [0, 2752 + H) fully contains fp
[2696, 2696 + H) — inherited bytes are all copied; the "lost ymm tail"
claim is also false. (The copy reads 56/32 bytes of trailing slab slack
beyond fp in child/parent; pre-existing, benign.)

Defense-in-depth note, not a defect: under a hypothetical
`CONFIG_HARDENED_USERCOPY=y` the generic fallback whitelist
(`include/linux/sched/task.h:185-191`, UML has no arch override) covers
[offsetof(thread)=2200, 2752 + H), which contains fp with 56 B margin. The
shipped builds have `# CONFIG_HARDENED_USERCOPY is not set` and
`# CONFIG_FORTIFY_SOURCE is not set` (both `kernel.config`).

**Verdict: no sizing change is required. C1 withdrawn in full; it is not a
blocker for the guest matrix. My thanks for the correction.**

## M2 RETRACTED — failed rt_sigreturn leaves fp[] fully defined in pristine
and patched alike; discard placement is byte-neutral and required

Mechanics verified in pristine 7.2.10:

- UML defines `INLINE_COPY_USER` (`arch/um/include/asm/uaccess.h:30`), so
  `copy_from_user` in `arch/x86/um/signal.c:97` expands to
  `_inline_copy_from_user` (`include/linux/uaccess.h:166-193`,
  alias at `:208`, wrapper at `:218-223`). It uses `copy_from_user`, not
  `__copy_from_user`.
- On every unsuccessful path — `should_fail_usercopy()`, `access_ok()`
  false, or a short `raw_copy_from_user` — it executes
  `memset(to + (n - res), 0, res)` (`uaccess.h:190`). The copied prefix
  holds user bytes; the entire uncopied suffix is zero-filled. The header
  states the contract at `:89`: "only copy_from_user() zero-pads the
  destination in case of short copy."

`check_copy_size` bypasses (`include/linux/ucopysize.h:44-61`) analyzed
against the actual destination, not hypothetically:

1. `__builtin_object_size(regs->regs.fp, 0) >= 0 && sz < bytes`: false —
   `fp` is a flexible array in a slab object whose allocation is not
   visible at the call site, so the compiler result is unknown (−1);
   FORTIFY_SOURCE is off in both shipped configs.
2. `bytes > INT_MAX`: false, `host_fp_size` is an XSAVE area (~2.7 KB).
3. `check_object_size` runtime slab rejection: compiled out
   (HARDENED_USERCOPY off); even if enabled, the whitelist spans fp with
   margin (see C1 math).

Hence after a faulting `rt_sigreturn` the whole `host_fp_size` destination
is deterministic user-prefix + zero-suffix — in the patched kernel
(discard marks EAGER, copy defines every byte; nothing reads fp[] between
the two statements) **and in pristine** (no discard, but the same
zero-padding copy overwrites the old authoritative image in exactly the
same way). `force_sig(SIGSEGV)` delivery then copies identical bytes into
the fault handler's frame. My assertion that pristine fp[] "stayed the
last authoritative image" on this path was wrong: the zero-padding contract
already destroyed that equivalence pre-patch. There is **no reachable
difference** from discard placement, and dropping ownership before the
wholesale overwrite is the correct ordering. **M2 withdrawn; no change to
sigreturn semantics is proposed or warranted.**

## Findings that stand (re-scoped: hardening/evidence, not blockers)

- **M3 (invariant hardening, unproven reachability).** The "cannot happen
  by construction" branches in `fp_lazy.c:330-349` (foreign-owner
  identity mismatch leaves descriptor LIVE pointing at the overwritten
  frame, later −EINVAL → `fatal_sigsegv()` via mcontext.c:309-311 and
  process.c:652-657) and `fp_lazy.c:390-397` (materialize flips EAGER
  without a copy) degrade to guest death or silent stale reads rather than
  self-healing. Static analysis could not construct a reachable
  interleaving given runner round serialization; recommendation is to
  convert these to an explicit salvage-or-panic invariant, not a claim of
  a current bug.
- **L4 (minor).** `uml_fp_handoff_reset` on x86_64 doesn't clear
  `handoff->lock` (i386 variant does) and leaves
  fp_frame/fp_size/fp_token on the cleared descriptor;
  `cleanup_broker_cache()` (mmu.c:106-131) lacks the reset that every
  destroy_context path has — currently safe only because store/hit reset
  first.
- **T1.** `tests/test_fp_patch.py:112` `@skipUnless(TREE.exists())` for
  `.work/lazy-tree` makes the strongest kernel-file checks silently skip
  against the frozen bundle; model↔applied-tree parity isn't
  bundle-reproducible.
- **T2 (real probe coverage gaps, distinct from the owner's six).**
  frame-churn cancel paths perform no FP assertion after the blocked
  threads resume (`fp_probe.c:528-562`) — the moved nested-frame slow path
  is exercised but not oracled; fork-fp never checks child-inherited bytes
  (`:658-690`); there is no coredump NT_X86_XSTATE ELF-note case.
- **T3/T4.** Model scope caveats (no enter−EINVAL path, no WARN-continue
  branches, i386 out of scope; duplicated dispatch at
  `fp_model.c:1067-1070`); the native ptrace skip is the probe's own
  zero-length query, not a container denial (my one completed bounded run:
  ordinary GETREGSET NT_X86_XSTATE succeeded, iov_len=2696).

## Corrected bottom line

The two correctness blockers I raised do not exist: task-struct sizing is
provably exact (32 B eager / 56 B lazy margin, compiler-measured), and the
failed-sigreturn path is byte-identical before and after the patch under
the kernel's mandatory copy_from_user zero-fill. No kernel change follows
from either. Residual items are one unproven invariant-hardening
suggestion (M3), two reset-path tidy-ups (L4), and genuine test/evidence
coverage gaps (T1-T4). Concurrency design findings from the original
report — handoff→descriptor lock order, strong-ref lifetime, one-copy
semantics, hook coverage — were not challenged and remain my static
assessment of correct.
