# BINAPI requests advance; CUDA initialization still fails

The owner-corrected kernel forwards the real 992-byte `0x20810108` BINAPI
request successfully on all four GPU paths: syscall return 0 and embedded
RM status 0, matching the native before/after runs. Existing control
features (31/31) and allocation cases (74/74) still match native results.
Four OS-event registrations and releases and four NUMA queries succeed.

Internal CUDA still fails at `cuInit(0)` with error 100. The four remaining
ioctl failures in this trace are `0x80028b`, one-byte parameters, syscall
errno 95. Native CUDA issues the same request but receives syscall success
and embedded RM status `0x56` (not supported). Returning a system-call
error instead changes the CUDA client's behavior. The next candidate is
limited to forwarding this observed one-byte, zero-flag form so the real
driver supplies its own result; no undocumented feature is fabricated.

The author's initial v11 BINAPI implementation was rejected before this
run. It tried to fit the child beside a 32-byte header in a 128-byte stub
buffer, imposing a 96-byte maximum despite the observed 992-byte request.
Its report incorrectly described the larger shadow-buffer capacity. The
owner replaced that child bounce with the existing same-address mapping
transport: snapshot the header, validate/prefault the guest range, submit
the header, and let the driver perform its own parameter copying. Only
prefault effort is capped at 1 MiB; the caller's size is passed unchanged.
`author-report.md` is retained as historical author material, not a
description of the owner-corrected implementation tested here.

Kernel: `718aa55193f97cc3ec406e475010e3cb26495a50905253fca172768b63e30b0a`.
Reproducible patch: `c77054411f905a25834bc038a72826da8f3e8901668412c7862543997bc3ca14`.
The patch reapplies exactly to the frozen baseline; 33 tracked source
hashes are unchanged during the owner rebuild. All 25 recorded process
groups are empty, guest shutdown is normal, and runtime inputs/libraries
are unchanged. Native before/after computation and transfer verification
pass on all four cards.

This is experimental kernel evidence. It does not establish internal CUDA
computation, full BINAPI error/size semantics, GPU lifecycle equivalence,
or performance. The logger captures only the first 64 child bytes of this
992-byte request; status success is not a full-payload comparison.
Run `python3 recheck.py` to verify the archive, patch replay and raw results.
