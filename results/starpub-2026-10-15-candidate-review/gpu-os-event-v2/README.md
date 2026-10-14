# Corrected native OS-event calibration on four GPUs

The corrected static probe exits 0 on all four A100 devices. Each run
allocates and frees nine distinct nonzero root clients through
`/dev/nvidiactl`, exercises the GPU character-device event operations,
and balances seven event allocations/frees and three device opens/closes.
Duplicate allocation returns embedded `0x1a`; missing/double free returns
`0x28`; empty GET_EVENT_DATA returns `0x59` and leaves the nonzero sentinel
untouched. Polls on blocking and nonblocking descriptors honor the supplied
zero/30 ms timeout. The malformed 15-byte request returns syscall EINVAL.

The original owner checker invocation omitted `--device` for cards 1–3,
so those three checker processes rejected only the mismatch against the
default `/dev/nvidia0`. The original invocations and outputs remain intact.
`owner-device-recheck.json` records a separate re-evaluation of the same
raw captures with each actual requested device. All four then pass the
unchanged checker. No source, case outcome, or capture was rewritten.

The strict static build, layout check and 36 fixture tests pass. All 14
command groups exited normally and are empty; frozen sources and the
before/after GPU inventory are unchanged. This calibrates a native ABI
probe. Internal Docker execution, actual notifications and CUDA computation
are separate validation work; none is accepted by this native result.

Run `python3 recheck.py` to verify hashes and replay the configured checker
on all four raw captures without accessing a device.
