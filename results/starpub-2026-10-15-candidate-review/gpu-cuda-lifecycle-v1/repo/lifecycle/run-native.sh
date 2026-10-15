#!/bin/bash
# Native build + GPU-free mock suite + native GPU run for the CUDA
# lifecycle probe, under ONE bounded exclusive-lock acquisition.
#
# Lock semantics (owner hardening):
#   * the global EX lock is taken once and held across compile, the
#     GPU-free mock/unit suite AND the GPU run/checker, so the actual
#     GPU phase cannot contend with concurrent owner experiments;
#   * wait is bounded to 180 s (flock -w 180) -- there is NO long
#     retry/sleep loop; if another holder runs longer, the operator
#     reruns explicitly;
#   * nesting is forbidden: do not invoke this wrapper from inside an
#     already CUDA_LIFECYCLE_LOCK_HELD context (including other locked
#     build scripts).  The parent may run the steps directly instead.
#
# GPU-only target.  The guest run reuses the existing
# scripts/run-gpu-cuda.py transport; this wrapper is never used in the VM.
set -eu -o pipefail
cd "$(dirname "$0")"
LOCK=/root/harddind-rebuild/kernel-build.lock
BIN=build/cuda-lifecycle-probe
FIXBIN=build/cuda-lifecycle-args-fixture
OUT=${1:-build/native.jsonl}
mkdir -p build

if [ "${CUDA_LIFECYCLE_LOCK_HELD:-0}" = 1 ]; then
    echo "run-native.sh: nested invocation forbidden (EX already held)" >&2
    exit 2
fi

# One bounded acquisition for the whole deliverable.  FD 9 is closed by
# the shell exit, which releases the lock; everything between this line
# and process end is serialized against other EX users.
exec 9>"$LOCK"
if ! flock -w 180 -x 9; then
    echo "run-native.sh: EX lock not acquired within 180 s; rerun later" >&2
    exit 2
fi
export CUDA_LIFECYCLE_LOCK_HELD=1

# ---- compile (probe + direct-function args fixture), single gcc each ---
gcc -O2 -Wall -Wextra -Werror -std=gnu11 -pthread \
    src/cuda_cuda_lifecycle_probe.c -lm -ldl -o "$BIN"
gcc -O2 -Wall -Wextra -Werror -std=gnu11 \
    tests/args_fixture.c -lm -ldl -o "$FIXBIN"

# ---- GPU-free mock/unit suite (no GPU call anywhere) -------------------
"$FIXBIN" | tee build/args-fixture.log
python3 -m unittest discover -s tests \
    -p 'test_cuda_lifecycle_checks.py' | tee build/selftest.log

# ---- native GPU run (still inside the EX region) ----------------------
UUIDS=$(nvidia-smi --query-gpu=uuid --format=csv,noheader,nounits \
        | paste -sd, -)

set +e
timeout 180 "$BIN" --deadline-ms 30000 | tee "$OUT"
probe_rc=${PIPESTATUS[0]}
set -e

# The native log is the parity reference the owner later passes to the
# guest checker via --native-reference.
set +e
python3 scripts/cuda_lifecycle_checks.py "$OUT" --uuids "$UUIDS" \
    | tee build/native-check.json
check_rc=${PIPESTATUS[0]}
set -e

echo "probe_rc=$probe_rc checker_rc=$check_rc"
[ "$probe_rc" = 0 ] && [ "$check_rc" = 0 ]
