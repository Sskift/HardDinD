# Four-card OS-event ABI inside Docker

One static binary (`545c9db722a03518c9f5c0fb072aadfe299215b36bdcffb409f7a1cc432846ba`),
already calibrated in [native v2](../gpu-os-event-v2/README.md), ran on all
four cards before, inside and after the guest: 12/12 captures passed the same
checker with each actual device argument. The guest used mapping-v13 kernel
`23efcdd84f69104170b1c085eca57a257dae3b88a1fe194cb074dd3205c9c729`.

Each process created/freed nine distinct clients, allocated/freed seven OS
events and opened/closed three descriptors. Duplicate and missing events,
opaque numeric tags, independent clients, ignored hDevice, malformed size,
empty GET_EVENT and blocking/nonblocking poll with 0/30 ms timeouts matched
the native ABI. This does not exercise a delivered GPU notification.

All 19 parent commands exited 0, including cleanup. The guest and network
exited normally; input hashes were unchanged. The original audit covers 19
command groups; the separately timestamped postcheck also confirms the guest,
network and supervisor groups were empty. Raw stdout/stderr, exact source,
commands, hashes and checker are retained. Run `python3 recheck.py` to replay.

CUDA initialization remains blocked at private control `0x20808159` after
mapping-v13; compute, transfer, GPU memory lifecycle and GPU performance are
still incomplete. These event checks are not full GPU acceptance.
