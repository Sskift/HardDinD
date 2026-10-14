# REVIEW.md — round6 FP repair: experiment 0018 correction design

Status: **early design review** (pre-implementation). Supersedes the terminal
round2 candidate `reference/kernel/experiments/0018-um-lazy-xstate.patch`
(SHA256 632f802f…1bfd). Nothing here changes the fact that all guest
performance/functional evidence remains owner-scheduled; this round delivers a
rebuilt patch, static/build evidence and owner-runnable tests only.

## 1. Confirmed candidate defects and how the rebuild removes each

### D1. GP register regression (`COPY(R12..R15)` deleted)

The rebuild does **not edit `get_mc_from_regs()`/`get_regs_from_mc()` at all**.
All GP register copies, including `COPY(R12); COPY(R13); COPY(R14); COPY(R15)`
at `arch/x86/um/os-Linux/mcontext.c`, remain byte-identical to the current
owner tree (main `2ec9618`). Regression test: the guest probe has a tracer
case that writes distinct values into R12–R15 (and other callee-saved GPs)
via `NT_PRSTATUS` SETREGSET at syscall-exit stops and verifies them in the
tracee; plus a signal ucontext GP-edit case. A static test asserts the four
COPY lines are present in the patched tree.

### D2. False-pass ptrace "skip"

* The probe distinguishes three outcomes per case: `pass`, `fail`, `skip`.
  `skip` is emitted when the environment denies the operation
  (`PTRACE_TRACEME`/regset unsupported), never as `ok:true`.
* Probe exit code: `0` all pass, `2` = incomplete (one or more skips),
  `1` = real failure. `HARDDIND_FP_END status=` carries the same value.
* `fp-run-core.py` maps exit 2 → result **incomplete** (not passed, not a
  crash either) and only a full run with every named case `pass` can pass
  acceptance. A timeout is recorded `timed_out:true` and always fails.
* The same probe runs natively here for tooling sanity only; its JSON is
  tagged `native_sanity` and is explicitly **not** kernel validation.

### D3. Unbounded salvage retention after task exit — removed by redesign

The salvage list is deleted entirely. New model (full description in §2):
per-task state lives in the existing `struct uml_pt_regs`; per-runner state is
one fixed handover record embedded in `struct mm_id`. When a runner frame has
to be reused while its owner is parked, the owner's **own `regs->fp` buffer**
receives the bytes (that buffer is otherwise stale while the frame is
authoritative). Synchronization is a runner handoff lock plus a per-task lock.

Reclamation is now exact and O(live tasks), never O(dead tasks):

* task exit: `exit_thread()` clears the task-side ownership under the task
  lock **without dereferencing any mm** (safe even after `exit_mm()` has run);
* mm teardown: `destroy_context()` clears the runner-side owner for `id`,
  every `cpu_ids[]` and `gpu_id` before freeing their stub stacks — also
  covering broker-cache transfer (the broker never executes guest code, so
  its handoff record is always empty; it is reinitialised on cache hit);
* the only bytes ever retained are: one XSTATE image per live runner (in its
  fixed frame) plus the normal `regs->fp` image inside each live task_struct.
  No heap nodes exist, so there is nothing to leak or accumulate.

A thread-exit-churn probe case (hundreds of short threads with distinct FP
images over a storming long-lived thread) plus VmRSS instrumentation covers
this; the host model test asserts the bound algebraically.

### D4. Gratuitous allocation + OOM panic — no allocation exists anymore

The hot path contains **zero** `kmalloc`/`GFP_*` calls and no `panic()`. All
bookkeeping is embedded: 40 bytes in `uml_pt_regs` (inside the already
dynamically-sized task struct; `arch_task_struct_size` math unchanged),
24 bytes per `mm_id` (`id` + `cpu_ids[NR_CPUS]` + `gpu_id`; ~1.6 KB per
64-CPU mm_context). Feature-on or off, nothing is allocated; memory pressure
has no failure path beyond the existing frame-copy behaviour. The only error
returns are genuine protocol violations (`-EINVAL`, same handling as a
corrupt stub frame today → `fatal_sigsegv()` in `userspace()`).

### D5. Excluded parallel runners / GPU series — stacked against current HEAD

0018 is rebased onto the **current full series** and is ordered last:
base `0001,0011,0014,0015` + experiments
`0002,0003,0004,0005,0006,0007,0008,0009,0012,0013,0016,0017,0019,0020,0021,0022,0023,0025`
+ `0018`. The design is a native superset of the parallel model:

* handoff state is per `mm_id`, so parallel mode has one independent record
  per vCPU runner (`cpu_ids[]`), plus inert records for the broker and the
  GPU worker (neither executes guest code);
* cross-CPU task migration (task armed on runner R1, resumes via runner R2)
  is an explicit slow path: collect from the home frame under R1's lock, then
  write R2's frame — the same two copies eager mode would do;
* mapping/GPU queued stub work was audited: `do_syscall_stub()` and the GPU
  batch path run **inside** the parked SIGSYS handler with
  `restart_wait`/`goto restart_wait` and never execute `rt_sigreturn` or
  touch the frame's mcontext/fpstate bytes, so no reconciliation is needed
  there; the fast path is safe across those windows;
* broker cache reuse (0007) resets the handoff record; runner death leaves
  the embedded record in the mm_context where `destroy_context()` clears it;
* `lazyxstate=1` is independent of `seccomp_threads`, GPU bridges/async wait
  and multicore; none of signals, ptrace, GPU or parallel runners are
  disabled or special-cased. No syscall-number whitelist exists.

## 2. Ownership design (replacement for the salvage list)

Two states per task, stored in `struct uml_pt_regs` (plain integer/pointer
fields only — the header is shared with USER_CFLAGS units):

* `FP_EAGER` (0): `regs->fp` is authoritative (today's semantics; also the
  state at boot, after materialise/discard/fork/exit and whenever the feature
  is off);
* `FP_LIVE`: authoritative bytes are the parked signal frame `fp_frame` on
  runner `fp_home`, validated by a monotonic per-runner token recorded at
  arm.

Per runner (`struct mm_id`): owner pointer (identity comparison only), token,
a one-word test-and-set lock. Per task: the descriptor above plus a one-word
task lock. Locks are implemented with `cmpxchg` + IRQ flag save/restore in
the kernel-side `arch/x86/um/fp_lazy.c` (the header files only see plain
`unsigned int` words; this matches series style — 0003 itself uses
`cmpxchg` in TLB code).

Transitions:

```
get  (get_stub_state_seccomp, host just wrote frame F on runner R):
    lazy off / i386 : memcpy F -> T.fp, state EAGER        (old path)
    lazy on         : lock R; require R.owner == NULL;
                      token = ++R.token; R.owner = T;
                      T = LIVE(R,F,size,token); unlock R   (no copy)

set  (set_stub_state_seccomp, frame G on runner R is about to be resumed):
    fast: T == LIVE(R,G), token matches
                      -> skip XSTATE memcpy; R.owner=NULL; EAGER
    slow: (1) if T LIVE on home H: lock H (+T lock); either copy its
              frame into T.fp itself or observe it was already pushed back;
              H.owner=NULL; EAGER; unlock
          (2) lock R; if some other task A owns R's frame, take A's task
              lock, memcpy G -> A.fp, A = EAGER, R.owner=NULL; unlock
          (3) memcpy T.fp -> G (caller); EAGER

materialise (guest signal delivery, ptrace/coredump FP/XSTATE get AND set,
             fork arch_dup_task_struct):
    LIVE -> lock home (+task lock); frame -> regs->fp; owner=NULL; EAGER
    otherwise no-op

discard (guest sigreturn copy_sc_from_user, exec flush_thread):
    LIVE -> drop runner ownership WITHOUT copying (bytes are overwritten);
    EAGER

exit  (exit_thread, may run after exit_mm -> no mm dereference):
    task lock only; T = EAGER, fp_home=NULL.  A concurrent runner owner
    either finishes first (bytes harmlessly land in the dying task's still
    allocated fp buffer) or observes EAGER and just clears the runner owner.
    destroy_context() guarantees runner records are cleared as well.

fork (arch_dup_task_struct before the task-struct copy; copy_thread reset
      after it): materialise parent; child descriptor reset + lock word 0.
```

Lock ordering is globally one direction: **runner lock → task lock**. Exit
takes only the task lock and never a runner lock; `set()` never holds two
runner locks simultaneously (home is released before the current runner is
locked). IRQ-safety: all runner/task lock sections run in `userspace()`
contexts; UML IRQ handlers (SIGCHLD, GPU completion socket, IPI, timer) never
touch handoff state, so no deadlock is possible against them.

Frame identity is the actual `_xstate` pointer derived from the current
`mctx_offset` every round, so nested/SA_NODEFER signal frames at different
altstack offsets naturally take the slow path instead of being matched by
address. GP regs, FS/GS base sync, TF flag and i387 conversion are
completely untouched by the optimisation.

## 3. Feature controls and scope

* `lazyxstate=0` default; `lazyxstate=1` opt-in via `__uml_setup` (invalid
  values rejected at boot). i386 always uses eager copies (i387 conversion).
* seccomp-off/ptrace backend never invokes the hooks (only
  `get/set_stub_state_seccomp` in the seccomp branch of `userspace()` do).
* Boot probe in `start_up.c` keeps the eager `get_stub_state()`.

## 4. Validation plan (evidence boundaries respected)

Done in this round (static/native only):
1. pristine 7.2.10 + full current series + 0018 applies fuzz=0 via an
   idempotent applier; R12–R15 static assertions;
2. UML builds (eager = full series incl. 0017/0023/0025; lazy = same +
   0018) with `runtime.config`, `-j4`, under the global
   `/root/harddind-rebuild/kernel-build.lock`; stub relocation check;
3. host `fp_model` pthreads model: randomised schedules over tasks/runners/
   CPUs, migration, frame churn, materialise/discard/fork/exit, byte-exact
   assertions and retention-bound check (provenance: behavioural model,
   clearly not kernel evidence);
4. native sanity compile/run of the guest probe with pass/fail/skip exit
   semantics demonstrated on a skipped facility;
5. python static tests for patch/applier invariants.

Owner-scheduled (NOT run here; harness delivered):
`fp-make-root.sh` + `fp-run-core.py --fp-suite` on `--cpus 4`,
both `seccomp_threads=0` and `=1`, with/without `lazyxstate=1`, plus the GPU
kernel configuration; controlled corebench A/B for performance claims.
Negative/skip results are preserved on disk as incomplete evidence.

## 5. Open items tracked to REPORT.md

* Exact guest build of the probe under the guest root toolchain (owner runs).
* Guest-measured memory bound and A/B performance numbers.
* AMX dynamic-state frame-size variation is handled by per-round size
  tracking but cannot be exercised on this host; noted as untested.
