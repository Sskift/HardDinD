# 0024 GPU mapping bridge — v10 char-fd event + NUMA/DETACH report

Built on the owner-frozen v7 control fix; source ownership returned after
the v8/v10 actual runs. No VM/GPU/performance was run by the author; all
verification below is bounded compile + deterministic host models. Actual
native/guest regression is owner-run.

## This round's corrections (from owner v8/v10 evidence)

### OS events rewritten to the 550.120 char-fd model
The prior implementation wrongly required params.fd to be a guest eventfd
(fget + eventfd_ctx_fileget), created a worker eventfd and a relay slot,
and synthesized a duplicate status 0x1F. Actual CUDA (v10 trace) passes
fd=16 = the same /dev/nvidia0 char fd the ioctl runs on. Pinned proof:
escape.c:679-708, osapi.c:463-595, event.c:173-180.

- `fd` is an **opaque u32 correlation tag**, never fget'd, never an OS
  descriptor. ALLOC_OS_EVENT stores `{hParent=hClient, nvfp, fd}`; the only
  uniqueness rule is (hClient, fd) on that nvfp.
- ALLOC/FREE_OS_EVENT now forward the 16-byte block verbatim; the worker
  driver owns all status. `RmAllocOsEvent` maps **any** allocate failure
  (including duplicate) to `NV_ERR_INSUFFICIENT_RESOURCES` (0x1A) — the
  synthetic 0x1F path was removed. Free miss → NV_ERR_INVALID_EVENT (0x28).
- NV0005 (class 0x79) `.data` is the same opaque tag, forwarded **verbatim**
  (no ~0, no worker-fd rewrite); full header copyback preserved for both
  NVOS21/64. Removed all eventfd lookup/fget around it.
- Added `NV_ESC_RM_GET_EVENT_DATA` (nr 0x52): NVOS41 16-byte block,
  `pEvent` nested pointer to the 16-byte NvUnixEvent output record,
  screened/bounced like other nested pointers.
- Removed the per-event eventfd bookkeeping (`gpu_event_map`, guest ctx
  map, relay EVENTFD slots for OS events). Interrupt readiness still
  reaches userspace via the existing device-fd poll relay; the kthread
  now only mirrors device level readiness.

### NUMA / DETACH (kept from v8, corrected per exact-ABI note)
- `NV_ESC_NUMA_INFO` dispatches at **case 215 (0xd7)** (`_IOC_NR` is the
  raw 8-bit value; earlier WIP used 15 and never matched). Card-only,
  exact 560-byte `nv_ioctl_numa_info_t`. Offline struct naturally
  u64-aligned (520 bytes); static_asserts placed after definitions.
- `NV0000_CTRL_CMD_GPU_DETACH_IDS` (0x216) flat control, 128-byte
  32×NvU32 reference-aware table.
- `0x20810108` (992) stays **fail closed** — layout unpublished; the
  catalog expert owns that investigation. No guessed forwarding.

## Verification (bounded, Starpub, global EX, max 4 jobs)
- Kernel build exit 0, **zero warnings**; stub code 1830 ≤ 4096 bytes.
- 8/8 unittest models; new `os_event_model` (opaque tag, dup=0x1A,
  free-miss=0x28, NV0005 scoping), `numa_detach_abi_model` (request decode
  + driver-header/kernel-mirror layout comparison), plus relay/VMA/RM/UVM.
- 8/8 seccomp BPF-interpreter + real-kernel extracted-filter combos.
- 0024 patch applies to immutable baseline v4 and round-trips byte-identical
  on all 14 files.

## Identity
- kernel linux/vmlinux sha256:
  `3f3f97080af4fbb92662d346e9cd39016a1722ae39e83ed35d827f1fe12f7380`
- gpu.c sha256:
  `fabb3c30b4df1d49e981b0b48c4043e04511f7ed4752c895592122c08cb612e2`
- 0024 patch sha256:
  `8024af295745930c6ca4fd582ebe657f8c1ab7a851feab369832d23a43b0f7d4`
- Full set: `.work/identity-v10/{binary,source14,config,patch}.sha256`,
  build log `successful-build.log`.

## Preserved / not regressed
Owner 0017 async wait; 0025 d04 / 0027 0x1f0 combined flags+size exception
(v7 feature 31/31 + alloc 74/74); 0026 typed cb33 FINN/rights; single
immutable snapshot; embedded-descriptor fget/fput outside gpu_lock; UVM
44/45=24, 46/47=40; dual-cdev device registration; VMA lifecycle.

## Remaining gaps (unchanged, explicit)
- Four BINAPI controls incl. 0x20810108 fail closed (catalog expert).
- UVM numeric requestedBase not additionally TASK_SIZE screened.
- SIGBUS (not native-equivalent) for guest kernel access to host GPU VMAs;
  no madvise UVM mirroring.
