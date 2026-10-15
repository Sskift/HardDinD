#!/usr/bin/env bash
# One build+test group under a SINGLE acquisition of the global EX lock:
# compile archive+harness, then run the harness, all while fd 9 is held once.
# Invoke inside a dedicated process group (setsid) under a 175s outer timeout.
#   usage: run_group.sh <label> <srcdir> <outdir> [asan]
set -euo pipefail

LABEL=${1:?}; SRC=${2:?}; OUT=${3:?}; VARIANT=${4:-release}
V2=$(cd "$(dirname "$0")/.." && pwd)
LOCK=${HARDDIND_KERNEL_BUILD_LOCK:-/root/harddind-rebuild/kernel-build.lock}
LOGS=$V2/logs
mkdir -p "$OUT" "$LOGS"

echo "group=$LABEL pgid=$$ variant=$VARIANT start=$(date -u +%FT%TZ)"
exec 9>"$LOCK"
flock -w 180 -x 9
export HARDDIND_BUILD_LOCK_FD=9
echo "group=$LABEL lock_acquired=$(date -u +%FT%TZ) inode=$(stat -L -c '%d:%i' /proc/$$/fd/9)"

SANENV=()
[ "$VARIANT" = "asan" ] && SANENV=(FRAG_SANITIZE=asan)
BIN=$OUT/frag-repro-v2

env "${SANENV[@]}" bash "$V2/tests/build_v2.sh" \
    "$SRC" "$OUT/libslirp.a" "$BIN" 2>"$LOGS/$LABEL.build.stderr" |
    tee "$LOGS/$LABEL.build.stdout"

set +e
if [ "$VARIANT" = "asan" ]; then
    ASAN_OPTIONS=detect_leaks=1:abort_on_error=0:halt_on_error=1 \
        taskset -c 0-3 timeout --kill-after=5 60 "$BIN" \
        >"$LOGS/$LABEL.tests.stdout" 2>"$LOGS/$LABEL.tests.stderr"
else
    taskset -c 0-3 timeout --kill-after=5 60 "$BIN" \
        >"$LOGS/$LABEL.tests.stdout" 2>"$LOGS/$LABEL.tests.stderr"
fi
rc=$?
set -e
echo "$rc" > "$LOGS/$LABEL.rc"
echo "group=$LABEL tests_exit=$rc end=$(date -u +%FT%TZ)"
# Lock released once, on process exit.
exit "$rc"
