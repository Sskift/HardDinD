# NVIDIA OS-event ABI regression probe — v1 (mapping-owner-v10 follow-up)

Status: **code complete; host compile, layout selfcheck and parser/model
tests only. No GPU/VM invocation by the author.** The owner schedules the
actual native and guest runs. This probe does not modify the backend or
the existing lifecycle implementation.

## Why

The mapping v10 guest cuInit fails (`CUDA error 100`) while
alloc/features probes pass. The guest bridge treats the
`NV_ESC_ALLOC_OS_EVENT` `fd` field as an eventfd descriptor
(`gpu-mapping-owner-v10/0024-um-gpu-mapping-lifecycle.patch`,
`gpu_alloc_os_event`). Pinned 550.120 sources prove `fd` is an **opaque
equality tag — CUDA passes its numeric GPU character-device fd number,
not an eventfd** — and pin the exact allocation/free/duplicate/missing
status channel. This probe records that native ABI so the mapping can be
regression-tested before another cuInit attempt.

## Files (new only)

| File | Purpose |
|---|---|
| `src/gpu_os_event_probe.c` | Standalone raw-ioctl OS-event ABI probe |
| `scripts/gpu_os_event_checks.py` | Strict independent JSONL checker (layout + native modes) |
| `tests/test_gpu_os_event_checks.py` | 28 parser/model + real-binary layout tests |
| `tests/run_os_event_host.sh` | Bounded EX-locked host build/selfcheck/test entrypoint |

Nothing under `/root/udock`, no backend, no existing source touched.

## Pinned ABI (driver 550.120, `arkclaude/reference/nvidia-550.120`)

- `nv_ioctl_alloc_os_event_t` / `nv_ioctl_free_os_event_t`
  (`src/nvidia/arch/nvalloc/unix/include/nv-ioctl.h:69-84`):
  `{u32 hClient, hDevice, fd, Status}`, 16 bytes, `_Static_assert` at
  offsets 0/4/8/12. Escapes 206/207, magic `'F'`.
- Dispatch `escape.c:679-703`: wrong payload size fails the **syscall**
  (`ioctl rc -1/EINVAL`); RM failures return through the **embedded**
  `Status` with `ioctl rc 0`. The two channels are never conflated.
- `osapi.c:464-535`: global per-nv event list keyed by
  `(hParent == hClient, fd)`. Duplicate pair: inner
  `NV_ERR_INVALID_ARGUMENT` (0x1F) wrapped by `RmAllocOsEvent` to
  **`NV_ERR_INSUFFICIENT_RESOURCES` (0x1A)** embedded. Free of an unknown
  pair: **`NV_ERR_INVALID_EVENT` (0x28)** embedded. `hDevice` is never
  read. The tag is never dereferenced as a descriptor.
- `RM_GET_EVENT_DATA` (0x52, `NVOS41_PARAMETERS` 16 bytes,
  `nvos.h:1955-1959`) on an empty per-fd queue:
  `osapi.c:648-680` + `nv.c:4040-4078` return embedded
  **`NV_ERR_OPERATING_SYSTEM` (0x59)**, ioctl rc 0, `MoreEvents` stays 0,
  no event data copied out.
- `kernel-open/nvidia/nv.c:2196-2241` `nvidia_poll`: empty queue → 0
  revents; blocking poll waits the timeout, `O_NONBLOCK` skips the wait
  queue.
- Status codes from `kernel-open/common/inc/nvstatuscodes.h`.

## Probe behavior (owned resources only)

Opens `/dev/nvidia0` three times (two blocking, one `O_NONBLOCK`),
allocates fresh NV01_ROOT clients per case, and exercises, with full raw
JSONL evidence:

1. `alloc_free` — tag == numeric GPU char fd (exact CUDA behavior),
   embedded 0 both ways.
2. `duplicate_alloc` — same `(client,tag)` twice: second embedded 0x1A
   with syscall rc 0; one free releases the single registration.
3. `missing_free` — free without alloc: embedded 0x28.
4. `double_free` — free again after a real free: embedded 0x28.
5. `opaque_tag_not_fd` — tag 0x4F530001 verified absent from
   `/proc/self/fd`: alloc/free embedded 0 — proves no OS-fd requirement.
6. `two_independent_clients` — two opens/nvfp, distinct clients, same
   opaque tag: both registrations succeed, both free.
7. `hdevice_ignored` — `hDevice=0xDEADBEEF`: alloc/free embedded 0.
8. `get_event_data_empty` — embedded 0x59, MoreEvents 0, output buffer
   untouched.
9. `poll_blocking_empty` (~30 ms, revents 0) and
   `poll_nonblock_empty` (immediate, revents 0) — no spurious readiness
   from a registration.
10. `wrong_size_channel` — 15-byte payload: syscall rc -1/EINVAL, no
    embedded Status (explicit `null`).

Every event allocation has a matching free; all clients freed via
NVOS00 and all fds closed on every branch; counts are emitted and
checked. No global detach/reset, no other client's handles, no GPU
memory, no notification trigger.

**NV0005 (`NV01_EVENT`, cl0005.h) is explicitly `not_tested`**: its
allocation needs an exact `hSrcResource`/class/notifyIndex and a real
interrupt-producing trigger that cannot be expressed without
fabricating an event. `internal_cuda:0` — no CUDA/CUDA-event claim.

## Checker strictness (`gpu_os_event_checks.py`)

- layout mode: compile-time struct sizes/offsets, escape numbers and
  status codes must equal the pinned values;
- native mode: exact case set and per-record op order, request echo
  (nr/size/hClient/hDevice/tag/`tag_is_open_fd`), **syscall rc/errno vs
  embedded Status distinguished on every record**, 0x1A duplicate, 0x28
  missing/double-free, 0x59 empty GET_EVENT_DATA, opaque tags genuinely
  non-fd, poll emptiness and timing bounds, balanced allocations/frees,
  clients and fds, summary `status` never trusted, NV0005 stays
  `not_tested`. Missing/extra/duplicated records and any fabricated
  readiness fail.

## Bounded host verification performed here (EX lock, single gcc, no GPU)

```
tests/run_os_event_host.sh
```

- strict compile under `/root/harddind-rebuild/kernel-build.lock`
  (`build/event-compile.log`, `gcc rc=0`);
- `--layout-selfcheck` validated by the checker (`build/event-layout.log`,
  `ok:true`);
- 30 unit tests OK (`build/event-selftest.log`), including real-binary
  `--layout-selfcheck` and explicit distinct-handle/shared-tag checks;
- existing lifecycle suite still OK (66 tests); no backend/VM/GPU call.

## File hashes (sha256)

```
dd6305f1f775e33ced402842b696f1a4bd46da0e01c3f10b8eb697e5141b2fe2  src/gpu_os_event_probe.c
afba8be0965215f0d2a0938fcea14603aaf1fab52c11304dc774e3a821be2daa  scripts/gpu_os_event_checks.py
a2042e264ce3659c96ac683712b93cba731577810693ddfb021719decc9293a4  tests/test_gpu_os_event_checks.py
9d0aeee271e7c777a1a4e221afa482550ac2e29bc55d8388b5084c6c02770fca  tests/run_os_event_host.sh
3aea7b5ba35ae586117405e2f4a38112cb1c3c3303090bb06f99b08dc43070df  build/gpu-os-event-probe
46ba7b16aff19e46ff689100fcc89efbcd066da791ade1b672f111ada05ef281  build/event-compile.log
6a5973eafbe9c8c9370f70cfca05b5374fa55b77afd9c8890decb505d8e787fb  build/event-layout.log
77143271f5d6e6c8943d7a30bd0d6d82c2ac989b1b3cadb6e382dd77742fc499  build/event-selftest.log
ef813e293fe8f994a05a691536ffe22b9427cab4935e71bc0a8f012b29bd44e5  build/event-host.log
```

## Owner run commands (serialized real GPU; not run here)

```
# Native four-A100 host (default /dev/nvidia0; 30 ms bounded poll)
build/gpu-os-event-probe --device /dev/nvidia0 --poll-ms 30 \
    | tee /results/gpu-os-event-native.jsonl
python3 scripts/gpu_os_event_checks.py /results/gpu-os-event-native.jsonl

# Guest: same binary inside the guest against the pass-through GPU node,
# then compare its JSONL with the checker using the same expected set.
build/gpu-os-event-probe --device /dev/nvidia0 \
    | tee /results/gpu-os-event-guest.jsonl
python3 scripts/gpu_os_event_checks.py /results/gpu-os-event-guest.jsonl
```

Acceptance is the checker verdict on native and guest: identical embedded
statuses (including 0x1A duplicate, 0x28 missing/double free, 0x59 empty
GET_EVENT_DATA), EINVAL only on the wrong-size syscall channel, empty
polls, and balanced owned cleanup. No performance threshold.
