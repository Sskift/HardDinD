#!/usr/bin/env bash
# Bounded native (host) checks for experiment 0018. These are tooling/model
# checks ONLY - never kernel evidence. Every invocation is wrapped in timeout
# so a lock inversion aborts as a failure instead of hanging.
#
#   fp-native-tests.sh [work-dir]
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$repo/.work}
mkdir -p "$work/native"
BIN=$work/native
T=${TEST_TIMEOUT:-60}

# Native subprocess tests share the machine; serialise bounded subprocess
# builds/runs on the global builder exclusion lock (shared, so other agents'
# builders can still work) and cap compiler jobs.
exec 9>/root/harddind-rebuild/kernel-build.lock
flock -s 9
MAKEFLAGS=-j4
export MAKEFLAGS

echo "== build: state-machine model =="
gcc -O2 -Wall -Wextra -pthread "$repo/src/fp_model.c" -o "$BIN/fp_model"
echo "== build: owner ASAN repro =="
gcc -g -O1 -fsanitize=address -fno-omit-frame-pointer -pthread \
    "$repo/tests/lifetime/lifetime_repro.c" -o "$BIN/lifetime-repro"
echo "== build: ASAN model =="
gcc -O1 -g -fsanitize=address -fno-omit-frame-pointer -pthread \
    "$repo/src/fp_model.c" -o "$BIN/fp_model_asan"
echo "== build: guest probe (static) =="
gcc -O2 -Wall -Wextra -pthread -static \
    "$repo/src/fp_probe.c" -o "$BIN/fp_probe"

echo "== owner lifetime counterexample under ASAN =="
timeout "$T" "$BIN/lifetime-repro"

echo "== model: individual modes =="
for m in --repro --null-drop --reuse --teardown --interleave \
         --concurrent --relinquish --feature-off; do
    timeout "$T" "$BIN/fp_model" "$m"
done

echo "== model: full schedule =="
timeout "$T" "$BIN/fp_model"

echo "== model: ASAN modes =="
for m in --repro --null-drop --reuse --teardown --interleave \
         --concurrent --relinquish --feature-off; do
    timeout "$T" "$BIN/fp_model_asan" "$m"
done
echo "== model: ASAN full schedule (scaled) =="
FP_MODEL_ROUNDS=${ASAN_ROUNDS:-15000} timeout "$T" "$BIN/fp_model_asan"

echo "== guest probe natively (tool sanity; ptrace case may be incomplete) =="
set +e
timeout "$T" "$BIN/fp_probe" >"$BIN/probe.json" 2>"$BIN/probe.err"
rc=$?
set -e
case $rc in
    0) echo "probe: all cases pass" ;;
    2) echo "probe: INCOMPLETE (skips recorded in $BIN/probe.json)" ;;
    *) echo "probe: FAIL rc=$rc"; cat "$BIN/probe.err"; exit 1 ;;
esac

echo "HARDDIND_FP_NATIVE_TESTS OK"
