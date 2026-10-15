# CUDA lifecycle: native controls pass, Docker thread phase times out

This is an unsuccessful internal-container acceptance run, preserved unchanged.
Later [native v5](../gpu-cuda-lifecycle-v5/README.md) exposes two start-gate bugs
in this frozen probe. The positive overlap measured in the v3 native streams
remains real, but the gate did not enforce that all four workers were ready
before release. The thread stall and missing internal phases remain failures.
Both native controls pass the frozen strict checker: four GPUs, eight solo
cycles per GPU, four concurrent threads with four cycles each, four fresh-exec
children with two cycles each, and all 12 directed peer transfers. Every
calculation and transferred result is checked. The internal Docker run completes
the four solo workers (32 cycles, 96 device allocations and matching frees),
then produces no thread records before the 180-second command timeout. Thread
records are emitted after joins, so their absence does not establish that every
individual thread made no progress. Children and peer phases are not reached.

The Docker command is killed with exit -9 and its checker rejects the missing
thread/child/peer/summary records. Docker remains responsive to `ps` and `rm -f`;
the VM subsequently shuts down normally. All 19 tracked process groups are empty
and the recorded inputs remain unchanged. Overall owner exit status is 1.

The probe is the preceding frozen v2 owner input plus the one-line
`owner-report-receipt-sentinel.patch`: a report receipt initialized to -1 must
be tested with `< 0`, not `!value`. This records the actual receipt time; it does
not repair or replace earlier raw evidence. The historical `firstbyte_ns` field
means newline-complete report receipt. Probe SHA-256:
`f944fe5f2895f379cdca84c28dba4f4a6fbfac97cf27777b4fbb5158310707f9`.

The kernel is the previously checked completion-ownership build,
`b8a58b855edb97bb8cf06bd99ef26fb7a63f08e0e38ba07fa1dcb8f915c1b502`.
Four vCPUs and asynchronous GPU waits remain enabled. GPU devices are supplied
explicitly with `--device`; this does not verify the standard `--gpus` path.
The base disk and older network backend are kept fixed for this comparison.
This run does not establish performance parity, broader CUDA API coverage, or
the cause of the thread stall.

Late diagnostic snapshots are also retained. `thread-stall-live-v1.json` loses
per-thread rows when reading `/proc/.../syscall` fails; its empty arrays must not
be treated as evidence of absent threads. The v2 snapshot retains six kernel
threads and explicitly records syscall/stack permission errors. Mconsole accepts
`sysrq w` and `sysrq t`, but the console contains headings without useful guest
stack frames. These snapshots neither prove nor disprove a kernel lock cycle.
They also make this a diagnostic run, not an uninstrumented performance sample.

Preflight: 45 argument fixture checks pass; 56 Python tests pass and six emitter
fixture tests are skipped because that producer fixture was unfinished when the
source was frozen. Those six skips are not passes.

Run `python3 recheck.py` to verify the archive, source hashes, command outcomes,
cleanup, and all three raw streams with the frozen checker. No GPU is used by
this offline recheck.
