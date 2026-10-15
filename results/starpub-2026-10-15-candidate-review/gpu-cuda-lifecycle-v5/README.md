# Native calibration rejects missing four-thread overlap

The unchanged lifecycle probe exits 0 and verifies all computations, transfers,
allocations/releases, four fresh-exec children and 12 directed peer pairs. Its
strict checker nevertheless rejects this native calibration: the four thread
work intervals have no common intersection (`overlap_ns = -2980764`). The owner
correctly stops before starting a guest. There is no internal Docker result,
guest stack observation, or native-after control in this attempt.

Source inspection finds two defects in the probe's start gate. Before the
parent arms it, `total` is zero, so an arriving worker can release it immediately.
When the parent does arm it, `gate_arm()` increments `waiting` even though
`total` counts only successfully created workers. It can therefore release with
one worker still absent. This explains why the gate does not guarantee a common
start; no scheduling assumption is needed to identify those source defects.
The previous positive-overlap samples remain actual measured overlap, but they
do not establish that the gate enforced simultaneous readiness.

These are probe defects, separate from the internal-container stall already
observed in v3. Repair and deterministic gate tests are assigned separately.
The strict overlap check is retained; this raw failure is not rewritten. A
successor may continue a specifically labelled diagnostic after this one native
coexistence failure, while retaining a failed calibration and unsuccessful total
result. Such continuation cannot count as acceptance.

This attempt also fixes v4's isolated directory links and compiles/executes the
read-only `/proc` helper against the supervisor process. Every emitted line is
validated as JSON. Some host fields are permission restricted; that fixture
tests the reporting path, not the availability of guest stack frames.

All nine command groups are empty and frozen inputs are unchanged. Preflight
retains 45 argument checks and 56 passing Python tests / six skipped unfinished
emitter tests. Run `python3 recheck.py` for hashes and an independent rerun of
the frozen checker on the actual native stream.
