# Stronger FP lifetime checks: native and 12 guest configurations

The same strengthened static probe passed all 15 cases natively and in
all 12 guest configurations, with no skipped or failed cases. The new
assertions check FP/SIMD bytes after blocked threads wake normally, and
check inherited child bytes before the child writes its own FP state.
The kernels and the 12-cell matrix are unchanged from `../fp-matrix-v1`:
four eager cells, four lazy-binary cells with the feature disabled, and
four with the feature enabled, covering one/four guest CPUs and the
parallel runner setting.

All 15 command groups exited without forced cleanup and were independently
empty afterward. Source, disk and kernel hashes stayed unchanged. The
probe source is `4faa72efd038d15c7e3246609769ac305dfea4cc1ad932485ca339620b62d648`;
the shared native/guest binary is
`483b66e64a392b3c6947009cb9440c696022036c12d7141489c32fe883a02b8c`.

The previous preparation failure is retained in `../fp-matrix-v2`.
The owner also fixed disk preparation to copy the contents of the
baseline directory into a real private directory. Copying the baseline
symlink itself with `cp -a` would leave subsequent build/install writes
pointing into the shared baseline. This run verifies a separately built
disk; it does not replace either frozen kernel.

These results close two lifetime-oracle gaps, but do not establish native
ABI equivalence or performance. In particular, short XSTATE SETREGSET
still has different native and UML behavior. The probe's 28-byte FX
request is unaligned and only demonstrates the alignment gate; its old
"full-only" detail overstates what that subcase proves. Aligned-prefix,
raw errno and returned-length coverage is being added separately.
Core-dump XSTATE notes and invalid-image validation remain untested.

Run `python3 recheck.py` for an offline check of input hashes, raw case
records, completion markers and all recorded process groups.
