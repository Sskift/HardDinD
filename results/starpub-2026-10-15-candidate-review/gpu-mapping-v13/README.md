# Preserve native unsupported-result semantics; next CUDA blocker recorded

The observed `0x80028b` call now returns syscall success and embedded RM
status `0x56` on all four GPU paths, matching native before/after results.
This fixes the return-channel mismatch from v12. It forwards only the
observed one-byte, zero-flag form to the real driver; no private feature
response or successful status is synthesized.

CUDA initialization advances further but still ends with error 100. The
four syscall failures now recorded are `0x20808159`, 332-byte parameters,
errno 95. The pinned public source tree does not define this private
control's parameter layout. Full payload logging and the driver dispatch
path are being investigated before extending that operation. Internal GPU
computation is still unverified.

BINAPI `0x20810108` with 992-byte parameters remains successful four times.
The feature matrix passes 31/31 and allocation matrix passes 74/74.
All four NUMA queries and four OS-event allocations/releases succeed.
Native computation and transfer verification pass on four GPUs before
and after the guest run. The guest stops normally, all 25 recorded process
groups are empty, and runtime inputs and libraries remain unchanged.

Tested kernel SHA256:
`23efcdd84f69104170b1c085eca57a257dae3b88a1fe194cb074dd3205c9c729`.
The frozen patch reapplies exactly to the same pinned baseline. As in v12,
the retained author report predates the owner's transport corrections;
`owner-abi-review.md`, frozen sources and raw results describe this run.

Run `python3 recheck.py` to replay patch application and the actual result
records. This is experimental functional evidence, with no GPU lifecycle
or performance acceptance claim.
