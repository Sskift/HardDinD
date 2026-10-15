# Actual guest stacks establish GPU / mmap lock inversion

The unchanged lifecycle probe again completes four solo GPU workers, then
stalls in its thread phase and exceeds 180 seconds. Two successful read-only
guest task snapshots, approximately 122 seconds apart, identify the same lock
cycle. This establishes a concrete cause of this reproduction; it does not
claim that every earlier GPU failure has the same cause.

| Guest thread | Actual stack | Held lock / requested lock from fixed source |
| --- | --- | --- |
| 12 | `gpu_forward_block -> fault_in_readable -> raw_copy_from_user -> handle_page_fault -> down_read_killable -> rwsem_down_read_slowpath` | Holds this mm's `gpu_lock`; waits for its `mmap_lock` as reader |
| 14 | `vm_mmap_pgoff -> do_mmap -> mmap_region -> __mmap_region -> vma_close -> gpu_vma_close -> mutex_lock` | Holds the same mm's `mmap_lock` as writer; waits for `gpu_lock` |

Thread 12 is executing ioctl `0xc020464f`; thread 14 is replacing a 4096-byte
mapping with `mmap` flags `0x32`. Thread 13 also waits for `gpu_lock` in the
forwarder; CUDA poll threads 16–19 wait for it in `gpu_poll`. The main thread
waits on a futex. Both snapshots successfully read all ten thread stacks
(`errno = 0`); the generic `wchan` names are not used to infer lock identity.

The source binding is retained in `source/` and `lock-cycle-source-binding.json`:

- The built GPU bridge source is copied byte for byte from the frozen
  completion-owner-v1 build snapshot. `gpu_shadow_snapshot()` takes `gpu_lock`
  before its input copy and nested-pointer faults, and retains it through
  output copyback. `gpu_vma_close()` takes that same lock.
- `mm/util.c` takes `mmap_write_lock_killable(mm)` around `do_mmap()` and only
  unlocks after it returns. UML's fault path acquires the mm read lock.
- The four unchanged core source files are extracted from the SHA-256-verified
  Linux 7.2.10 archive. No patch in the held kernel's series changes them.

The repair under development moves faultable guest memory access outside
`gpu_lock`, using a private immutable request snapshot while preserving
serialization of the shared worker shadow and response. Merely prefaulting a
buffer is insufficient because another thread may change its mapping. Other
copyback and allocation-event paths need the same ordering audit. No repaired
kernel is tested in this record.

This is a **focused Docker-only diagnostic**, not paired native acceptance or
performance testing. The historical v3 native stream is explicitly named
`reference-native-before.stdout` and is used only for UUID/peer-contract
checking. V5's thread-overlap failure and v6's child-overlap failure remain
separate unsuccessful calibrations. The probe's start-gate defects are also
being repaired separately; they do not remove the requirement that real
concurrent GPU operations work without this kernel lock cycle.

The primary CUDA container keeps four vCPUs and asynchronous GPU waits enabled.
Only two separate `docker exec --privileged` observer processes receive extra
**guest** capabilities to read `/proc/1/task/*/stack`; no outer capability,
seccomp/AppArmor policy, host cgroup, or host Docker socket is changed. The
observer is the compiled `repo/lifecycle/src/proc_snapshot.c`. It records field
errors explicitly and is bounded by the owner command timeout. Its native JSON
fixture is checked before the guest starts.

Docker times out with exit -9; the frozen checker rejects the missing
thread/child/peer/summary records. `docker ps`, `rm -f`, and normal instance
shutdown succeed. All 17 owner-tracked groups plus the separately recorded
second observer group are empty (18 total); frozen runtime inputs are unchanged.
`second-observer.json` records that extra command and its cleanup, and
`post-run-audit.json` joins the final group evidence. The overall owner exits 1.

Run `python3 recheck.py` to verify the full archive, source binding, both
snapshots, command outcomes, cleanup, and rejection by the frozen checker.
