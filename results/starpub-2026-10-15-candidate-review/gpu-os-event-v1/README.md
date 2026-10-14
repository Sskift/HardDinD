# Native OS-event probe v1: failed calibration

The owner compiled the frozen source statically on Starpub and ran the
same binary separately against `/dev/nvidia0` through `/dev/nvidia3`.
All four probe runs and all four checker runs exited 1. This is a failed
native calibration, not evidence that the guest GPU bridge passes or fails.
The author report describes intended behavior before this actual run.

Every run failed to obtain any of its nine client handles. The probe did
not record the root-allocation ioctl's raw return fields and continued
with `hClient=0`. Consequently, the two purported independent clients
were the same `(client, tag)` pair: the second allocation returned embedded
status `0x1a`, and the second free returned `0x28`. Each run recorded
11 errors, zero created/freed clients, and six successful event
allocations/frees. The checker rejected these results.

The run also exposed an inconsistent `hdevice_ignored` checker expectation:
the probe intentionally frees with `hDevice=0`, while the checker expected
`0xdeadbeef` for both operations. Source review found additional probe
limitations: an errno read after descriptor enumeration, unknown descriptor
state being coerced to true, and a zero-filled output buffer used to infer
that no output was written. These are being corrected before another run.
The immediate nonblocking-descriptor poll requested timeout zero; it does
not establish that `O_NONBLOCK` overrides a nonzero poll timeout.

The strict static build and ABI layout check succeeded. All 13 command
groups were empty after normal exit, frozen input hashes stayed unchanged,
and the four-device inventory was identical before and after. Binary SHA256:
`0bbce9bf8dbe9803a90471c71605fa8d3160a053987ccc3de72abbd8c985ae3a`.
No guest, CUDA computation, notification trigger, or performance workload
was executed in this experiment.

`python3 recheck.py` checks the archive and replays the failed native
observations without accessing a GPU. `owner-report.json.status=finished`
means the supervisor finished; individual exit codes retain the failure.
