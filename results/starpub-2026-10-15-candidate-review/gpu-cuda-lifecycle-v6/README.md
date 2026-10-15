# Native calibration rejects missing four-child overlap

The probe exits 0 with all computation and transfer results correct, but the
strict checker rejects the four fresh-exec child work intervals: their common
intersection is `-527872 ns`. All 12 directed peer results are present. This is
a failed coexistence calibration; it is neither a CUDA computation failure nor
proof of a driver or kernel defect.

This diagnostic supervisor permitted continuation only after the specific
thread-overlap failure previously observed in v5. The different child-overlap
failure correctly stops it before guest launch. No internal GPU execution,
stack snapshot, or native-after result is available. All nine command groups
are empty, and frozen inputs remain unchanged. Normal guest shutdown is false
because no guest was launched.

Both native failures remain separate from the v3 Docker thread stall. Probe
barrier repair and concurrency coverage continue independently. The next
focused Docker-only run uses the unchanged probe to obtain blocked task stacks;
it explicitly has no current paired native calibration or performance claim.

Run `python3 recheck.py` to verify the archive, cleanup and independently rerun
the frozen checker against the native stream.
