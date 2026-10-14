# REPORT — experiment 0018 lazy XSTATE handoff, round6 (revision 4)

Date: 2026-10-14. Scope: source follow-up after OWNER-V3-SOURCE-FOLLOWUP.md,
on top of the frozen `/root/harddind-rebuild/fp-owner-review-v3` (left
untouched). All work inside
`/root/harddind-rebuild/arkclaude/round6/fp-repair/`; `/root/udock` and the
read-only owner tree were never modified. **No guest/UML, GPU or performance
measurement was executed by the agent.**

## 1. Follow-up items and disposition

1. **Double XSTATE copy (fixed).** The lazy slow path published
   `frame←regs->fp` inside `uml_fp_leave()` and returned 0, causing
   `set_stub_state_core()` to memcpy the full XSTATE a second time. The slow
   path now publishes under the handoff lock and returns the caller's
   "do not copy" value; mcontext copies only when `lazy == 0`
   (lazyxstate=0 eager semantics unchanged). Model instrumentation counts
   final frame writes per runner: fast path 0, lazy slow 1, feature-off 1 —
   enforced across ~1.46M rounds (lazy + feature-off schedules).
2. **Nohome materialize/discard ordering (fixed + argument).** Remote
   relinquish updates `(fp_home, fp_state)` together under the descriptor
   lock; the lockless entry in materialise/discard could observe a torn
   LIVE+no-home snapshot on a weakly ordered host and publish EAGER without
   pairing with the remote `frame→fp` copy. New `fp_read_coherent()` takes
   the descriptor lock for the snapshot at every reader (resolve_self,
   materialise, discard): an EAGER observation acquires-after the
   relinquish's release so the image bytes are guaranteed visible;
   LIVE+no-home under the lock is unreachable and warns. The snapshot lock
   is released before any handoff lock is taken, so order remains
   handoff→descriptor with no inversion.
3. **i386 static_assert scope (fixed honestly).** 24-byte assert moved
   inside `#ifdef __x86_64__`; the i386 `#else` asserts the actual ILP32
   layout (16 bytes = ptr 4 + token 8 + lock 4). i386 keeps always-eager
   stubs. No i386 cross toolchain exists in this workdir, so an i386 UML
   compile is explicitly owner-CI scope — not claimed as run.
4. **Kernel/model body parity (added).**
   `tests/test_fp_patch.py` extracts and compares ordered
   lock/publish/put token sequences of the six paired functions
   (fp_read_coherent, resolve_self/fp_resolve_self, leave, materialize,
   discard, handoff_reset) and checks handoff-before-descriptor,
   publish-under-lock, put-after-unlock. Intentional divergences documented
   in the model banner (3-way leave result for instrumentation; heap tasks
   vs task_struct; abort-on-spin vs relax).
5. The `current_mm_enter()` `preempt_disable()` fast-path note from the owner
   is acknowledged; no generic fast-path preemption bug is claimed or
   worked around.

## 2. New/changed evidence

* `--relinquish` model mode: 4000 barrier-synchronised races between a
  parked owner's materialise/discard (ptrace/coredump-style remote read)
  and another vCPU's frame takeover — asserts the parked image survives
  materialise, single frame publication, EAGER/clear end state, balanced
  refs. `--feature-off`: lazyxstate=0 schedule with exact one-copy/round
  accounting.
* Failed prior evidence preserved: `docs/failed-evidence/` (v1 ASAN UAF,
  v2 hung-model.json). The v2 deadlock (rev2 ABBA) cannot occur in rev3/4:
  the exit hook that inverted the lock order is deleted, and every model
  lock spin aborts at SPIN_CAP; all runs are invoked under timeout.

## 3. Artifacts and reproducible build identity

| artifact | sha256 |
|---|---|
| pristine source | `2feaa70453e91f541be6a5c9078d8168e081af45a3d9718054850538cff2ebf3` (`/root/harddind-rebuild/cache/linux-7.2.10.tar.xz`, pinned in sources.lock.json) |
| kernel/experiments/0018-um-lazy-xstate.patch | `59bd8b361ac69f36c8268aa3e4047c214ac3ecba0724b2cf56518a9a8281d2ca` |
| src/fp_model.c | `65b1a2dbafa93ac818a928a863c986d87a28fa06929f9238e957611dba77ec0b` |
| src/fp_probe.c | `b7fa7210e4eac21bb14f414a8d4e1fe39624a9aebf1722bb3ebbbabecb395db8` |
| tests/lifetime/lifetime_repro.c | `7fc9b4367d51854ddadbd810acb18c22fc5e248a6f097b8aafef1ad78fd7311e` |
| built kernel fp-eager | `a0bc79d9be2d61b0dbdef8bb34a0fad65f98d8046f196bf21ae663e47c503055` |
| built kernel fp-lazy | `9f957ded67f8ee74a6178372db7c7acda6aea5581d837603e786f8312a9f836e` |

Both builds share the identical resolved `.config`
`786db476de884c56ff90bc4df0646c8be8802d56798aaf696aab0cbd64c18a91`
(runtime.config requirements merged). Rebuild:

```
flock /root/harddind-rebuild/kernel-build.lock \
  JOBS=4 bash scripts/fp-build-kernel.sh <work> {eager|lazy|both}
```

Series applied to the eager tree: 0001,0011,0014,0015 base; GPU/parallel
chain 0003,0004,0005,0006,0007,0008,0009,0012,0013,0016,0019..0023,
0017 (owner order after 0023), 0025,0026,0027; the lazy tree adds 0018
last. 0002 is not combined with 0003. The eager tree's gpu.c is byte
identical to the owner's current tree. Both kernels pass
check-stub-relocations.py (passed:true); no compiler warnings on any
touched unit.

## 4. Test commands and results (bounded, this session)

```
python3 -m unittest discover -s tests                      # 42+ checks OK
timeout 55 bash scripts/fp-native-tests.sh                 # rc 0
```

Model battery (normal and ASAN builds, each mode timeout 60,
ASAN full schedule scaled FP_MODEL_ROUNDS=15000):
owner-repro, null-drop (strict task_put(NULL) aborts in the expected forked
child), address-reuse 3000, teardown-reuse, interleave ×4, exit-vs-takeover
4000, relinquish-race 4000, full lazy schedule, feature-off schedule,
born==freed bounds — **all OK, zero ASAN findings, zero failures**.
The native guest probe's ptrace case remains skip/INCOMPLETE in this
container (exit 2) — preserved as such, never a pass. Raw log:
`.work/native-rev4.log`; kernel build logs `.work/build-rev4*.log`.

## 5. Owner-scheduled, NOT performed here

Guest FP matrix (both kernels × cpus 1/4 × seccomp_threads 0/1 ×
lazyxstate 0/1): `scripts/fp-make-root.sh` + `scripts/fp-run-core.py ...`
(pass/incomplete/fail; timeout is fail). GPU acceptance incl.
gpu_bridge=1. Controlled corebench A/B before any performance statement.
Optional i386 UML cross build.
