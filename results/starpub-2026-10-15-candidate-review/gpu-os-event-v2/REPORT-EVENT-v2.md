# NVIDIA OS-event ABI probe — v2 (actual-native v1 repair)

Status: **code complete; host compile/layout/parser tests only. No
GPU/VM invocation by the author.** Owner reruns native, then Docker.
v1 frozen bundle (`/root/harddind-rebuild/gpu-os-event-owner-v1`) is
untouched; the actual native-v1 capture is copied (not moved) into
`tests/fixtures/os-event-v1-native-0.jsonl` and is now a regression
fixture.

## The actual-native v1 failure (immutable evidence)

All four A100 runs failed with 11 probe errors and
`clients_created: 0`. Every `new_client` returned zero, so all event
ioctls ran with `hClient=0`; `two_independent_clients` collapsed onto
one pair (second alloc embedded 0x1A, second free 0x28).

Root cause, pinned in 550.120 `escape.c:386-403`: **NV01_ROOT_CLIENT
allocation and RM free are `NV_CTL_DEVICE_ONLY`.** v1 issued the
NVOS64/NVOS00 root calls on `/dev/nvidia0`, which rejects them
(`-EINVAL`); the probe treated the generated-handle failure silently and
kept issuing events with client 0.

## v2 fixes (probe: `src/gpu_os_event_probe.c`)

1. **Correct device chain** — opens `/dev/nvidiactl` (root client) plus
   two GPU char fds (blocking and `O_NONBLOCK`). Nine fresh NV01_ROOT
   clients are allocated on ctl with raw NVOS64; events go to the GPU
   nvfp using those handles and the opaque numeric tag (GPU char fd
   number, CUDA behavior). Root free uses NV00 `(c,c,c)` exactly as the
   verified `gpu_alloc_probe.c`/`gpu_system_features_probe.c` do.
2. **Raw syscall records** — new `open/close`, `root_alloc` and
   `root_free` JSONL records carry raw rc/errno, embedded status, and
   every returned NVOS64 field (root/parent/object/cls/alloc/rights/
   size/flags). The client handle is pinned to `hObjectNew` (returned
   `object`), with `client_handle_field:"object"`; the record proves it
   is not `hRoot`.
3. **Fail closed on client failure** — if any root alloc fails, all
   dependent event cases emit explicit `skip` records (checker fails the
   run) and only successfully created clients are freed; **no event
   ioctl is ever sent with hClient 0**. nvfp-scoped cases (poll matrix,
   empty GET_EVENT_DATA, wrong-size) still run for isolation.
4. **Saved errno** — `event_call` captures errno immediately after
   ioctl; the JSON uses the saved value (the `/proc/self/fd` lookup and
   printf can no longer clobber it); same around poll and open/root
   calls.
5. **tag tri-state** — `/proc/self/fd` lookup unknown (`-1`) emits JSON
   `null` and fails, never `true`.
6. **GET_EVENT_DATA sentinel** — output buffer pre-filled with 0xA5;
   "untouched" is an exact byte comparison on a nonzero sentinel.
7. **Poll matrix per POSIX/pinned semantics** — four records:
   blocking and nonblocking fds × timeout 0 and 30 ms, each recording
   `fd_flags`/`fd_nonblock`, the requested timeout, saved rc/errno,
   revents and elapsed ns. timeout 0 returns immediately on both fd
   types; a 30 ms poll waits ~30 ms on the nonblock fd too
   (O_NONBLOCK does not override poll's timeout).
8. **hDevice fields explicit** — `hdevice_ignored` alloc sends
   `0xDEADBEEF`, free sends `0x00000000`; both intended values are
   checked per record (the v1 checker wrongly forced DEADBEEF on the
   free).

NV0005 remains `not_tested` (no exact safe trigger); `internal_cuda:0`.

## Checker v2 (`scripts/gpu_os_event_checks.py`)

- layout mode validates compile-time struct/offset/escape/status/
  device-chain constants;
- native mode requires: 3 opens/3 closes with matching fds and raw
  rc/errno; **9 distinct nonzero root clients** allocated and freed on
  ctl (syscall vs embedded channels distinct; hObjectNew identity);
  every event's hClient equals its allocated root handle (rejects
  client 0); per-record hDevice intent; opaque tags genuinely non-fd
  and the gpu-tag an open fd; two independent clients with distinct
  handles and one shared tag; empty GED 0x59 with untouched 0xA5
  sentinel; the four poll matrix timing/flag invariants; wrong-size
  strictly syscall −1/EINVAL with null embedded; zero skips; balanced
  nonzero accounting; summary never trusted;
- **the frozen actual-native v1 log is asserted to fail** and its
  concrete reasons (new_client errors, collapsed two-client pair,
  hDevice oracle, missing root_alloc) are named
  (`test_actual_native_v1_log_is_rejected`).

## Host verification (EX lock, single gcc per compile, ≤4; no GPU/VM)

- strict compile `gcc rc=0`, byte-reproducible binary
  (`build/event-compile.log`);
- `--layout-selfcheck` → checker `ok:true` (`build/event-layout.log`);
- **36** focused tests OK (`build/event-selftest.log`), including the
  frozen-v1-log rejection and real-binary layout/argv tests;
- full task test directory (lifecycle + event): **141 tests OK**;
- entrypoint `tests/run_os_event_host.sh` (`build/event-host.log`).

## Hashes (sha256)

```
4d540b6d94a8a0760723be2405bd19fdc951b8c0920a2d42c96e1e82bc84e70a  src/gpu_os_event_probe.c
4356b7ea8a94cdada34af28bef2b3d909b7fb45d3fcd338c9ffe632a9743be8c  scripts/gpu_os_event_checks.py
8620037ef617082eba2eb6651e961938274b30f485585b2c90f5a8533ad318f1  tests/test_gpu_os_event_checks.py
9d0aeee271e7c777a1a4e221afa482550ac2e29bc55d8388b5084c6c02770fca  tests/run_os_event_host.sh
510cb2761d7d50d39c9be240fdc7728b05988f569296cfd7f0f4b05762869eb9  tests/fixtures/os-event-v1-native-0.jsonl
74230267add3517175ba08b0787851d5b03bdb199cb2af949517341045e733f2  build/gpu-os-event-probe
46ba7b16aff19e46ff689100fcc89efbcd066da791ade1b672f111ada05ef281  build/event-compile.log
bd2427adac2d9c310d64d7d83678b318696ce89b815c0cabdc47d3339c56d6d1  build/event-layout.log
211e56c66ed93fbac8a6b2a30569e164a33d8e898b50bdad25cf076b2162ea6e  build/event-selftest.log
4d37a7500a26fc15759c060285f5e7fb62ea768000a76092eaf282a17a127366  build/event-host.log
```

## Owner rerun (native then Docker)

```
build/gpu-os-event-probe --device /dev/nvidia0 --ctl-device /dev/nvidiactl \
    --poll-ms 30 | tee /results/gpu-os-event-native-v2.jsonl
python3 scripts/gpu_os_event_checks.py /results/gpu-os-event-native-v2.jsonl
# guest (pass-through nodes), same checker invocation/expectations
```

Acceptance: checker `ok:true` on native and guest — 9 nonzero distinct
ctl root clients, embedded 0x1A duplicate / 0x28 missing+double free /
0x59 empty GED, EINVAL only on the 15-byte syscall channel, empty
four-matrix polls, and fully balanced owned cleanup.
