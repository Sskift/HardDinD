# CPU shares v6: complete evidence, allocation still indeterminate

The production output indexer now handles formatted JSON objects, scalar
`docker wait` output and strict probe JSONL separately. The owner added a
durable launch journal before each wait, supplementing the author's completed
command journal. 106 host checks ran without skips, including the previous
real-output finalization regression. The unchanged probe/checker then ran in
two real Docker containers on a four-vCPU guest.

All live updates and the graceful stop sequence completed. Both container
IDs stayed unchanged, CPU affinity and whole-run accounting checks passed,
and the final equal-weight phase passed. The initial unequal-weight phase
and reversed-weight phase remain **indeterminate** because observations have
gaps and insufficient clean joint windows. Ordering is not tested; neither
these results nor prior failed runs count as full shares acceptance.

A single A capture lasted **11.490377984 seconds**; B's maximum capture was
0.157118464 seconds, with additional gaps between samples. No CPU quota was
configured in this contention fixture. This observation rules out explaining
every stall solely by quota throttling; it does not by itself identify the
responsible syscall or scheduler path. The final equal-control fraction
interval was 0.493396–0.507439 under the existing functional checker. Its
diagnostic bands are not a user-approved performance tolerance.

The report, 46 launch records, 46 completed command records, all raw outputs,
input hashes and original author runner are preserved. The guest stopped
normally, all inputs were unchanged and 51 recorded groups were empty.
One report defect remains: the evaluator normalized update-bracket snapshots
in place before serialization, so direct evaluation of the saved evidence
fails. The original raw pre/post command outputs are intact. `recheck.py`
first verifies this historical failure, then restores only those six original
snapshots and reproduces all 17 original check rows exactly. It does not
overwrite or silently repair the saved report; a pure evaluator is pending.
Run `python3 recheck.py` to perform that explicit reconstruction and replay.
The owner supervisor returned 2 for indeterminate. Performance acceptance
and the full project goal remain false.
