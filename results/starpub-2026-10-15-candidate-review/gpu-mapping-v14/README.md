# Mapping v14: real CUDA computation inside Docker on all four GPUs

**Follow-up limitation:** the [first uninstrumented run](../gpu-plain-perf-v1/README.md)
fails with CUDA 304 during context creation. The successes below are the
recorded LD_PRELOAD + strace run; ordinary execution stability remains open.

The actual internal Docker process successfully initialized CUDA and computed
on each of Starpub's four GPUs. For every card it allocated three 4 MiB device
buffers, uploaded two arrays, loaded PTX, launched an addition kernel,
synchronized, downloaded the result and checked all **1,048,576 elements**.
It freed the buffers and destroyed its module/context. The same native
program before and after the guest also passed on all four cards.

This closes the initialization failure retained in v13. The change forwards
the observed private subdevice `0x20808159` 332-byte, flags-zero packet through
the existing same-VA driver transport, and adds the observed one-byte
`0x20808162` query. Full input/output captures precede the change in
[private-trace-v1](../gpu-private-trace-v1/README.md). No private C structure
was invented, no driver return status was synthesized, and other shapes of
the 332-byte command remain unsupported. Its broader undocumented ABI is
still a limitation.

| Evidence | Result |
|---|---|
| System-feature contracts | 31/31; native, guest and five internal Docker runs |
| Allocation contracts | 74/74; native, guest and five internal Docker runs |
| CUDA native-before / Docker / native-after | 4/4 GPUs in each phase |
| Target private control payloads | Complete 332/992/1-byte snapshots; four successful calls each per phase |
| Input/library hashes | Unchanged |
| Shutdown / cleanup | Normal; 25 recorded groups empty |

Kernel SHA256: `9306f4e6045bb26821338c6962f8c67f887ea6f86f917524ebe4b22a25a25927`.
Patch SHA256: `1ef604a47bf2399f6dcdde3664e5d2fa1a029ee96b1f303eb6577fdbe749dac6`.
The patch replays exactly over the frozen async+0027 baseline. The original
source, build records, raw ioctl/strace output and per-phase results remain
archived. `python3 recheck.py` verifies this binding and recomputes the
contracts from raw observations.

The optional main build target is `make kernel-gpu-mapping-experiment
WORK=<isolated-work> JOBS=4`; the numbered 0024 replacement must be applied
**after** the async+0027 series, as the selector does. The tested runtime uses
`--gpu-control-experiment --gpu-async-wait --memfd-ram --memfd-stub` and explicit
NVIDIA device forwarding to the inner container. The ordinary default kernel
and older GPU experiment targets are unchanged.

This is sequential four-card compute with a small allocation lifecycle.
Concurrent processes/threads, repeated/extended memory lifecycle, peer
transfers, normal `docker run --gpus all` integration and performance still
need verification. Logs and `strace` are enabled here, so recorded seconds
are diagnostic only. Full GPU and full Docker acceptance remain incomplete.
