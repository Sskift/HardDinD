#!/usr/bin/env bash
# Bounded native (host) checks for experiment 0018. These are tooling/model
# checks ONLY - never kernel/guest evidence. Every subprocess build/run is
# wrapped in `timeout`, and the whole script holds the GLOBAL exclusive
# builder lock for the duration (no nested flock anywhere in the called
# binaries). Compiler jobs capped at 4.
#
#   fp-native-tests.sh [work-dir]
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$repo/.work}
mkdir -p "$work/native"
BIN=$work/native
T=${TEST_TIMEOUT:-60}
ASAN_T=${ASAN_TIMEOUT:-60}

# One EXCLUSIVE global lock for the whole bounded run. fd 9 is held by this
# script only; nothing below re-flocks.
exec 9>/root/harddind-rebuild/kernel-build.lock
flock -x 9

# Exact cleanup of any child we spawn, including on error/timeout abort.
cleanup_children() {
	local p
	for p in $(jobs -pr); do
		kill -KILL "$p" 2>/dev/null || true
	done
}
trap 'cleanup_children' EXIT INT TERM

export MAKEFLAGS=-j4
CC=(gcc -Wall -Wextra)

echo "== build: state-machine model =="
"${CC[@]}" -pthread "$repo/src/fp_model.c" -o "$BIN/fp_model"
echo "== build: owner ASAN repro =="
gcc -g -O1 -fsanitize=address -fno-omit-frame-pointer -pthread \
    "$repo/tests/lifetime/lifetime_repro.c" -o "$BIN/lifetime-repro"
echo "== build: ASAN model =="
gcc -O1 -g -fsanitize=address -fno-omit-frame-pointer -pthread \
    "$repo/src/fp_model.c" -o "$BIN/fp_model_asan"
echo "== build: guest probe (static) =="
# -O2 is required: the ptrace tracee's single-asm sampling block needs the
# register allocator to satisfy many memory operands (fails at -O0).
"${CC[@]}" -O2 -pthread -static \
    "$repo/src/fp_probe.c" -o "$BIN/fp_probe"

echo "== owner lifetime counterexample under ASAN =="
timeout "$ASAN_T" "$BIN/lifetime-repro"

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
    timeout "$ASAN_T" "$BIN/fp_model_asan" "$m"
done
echo "== model: ASAN full schedule (scaled) =="
FP_MODEL_ROUNDS=${ASAN_ROUNDS:-15000} timeout "$ASAN_T" "$BIN/fp_model_asan"

echo "== guest probe natively (tool sanity; pass/skip/fail, never force pass) =="
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
