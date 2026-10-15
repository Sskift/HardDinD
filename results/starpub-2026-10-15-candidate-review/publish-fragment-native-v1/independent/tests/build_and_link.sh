#!/usr/bin/env bash
# Compile a frozen libslirp source tree into a static archive and link the
# direct-function reproducer. Bounded: must be invoked under the global EX
# build lock (or it acquires it once; never nests). Each compiler gets
# taskset -c 0-3 and a 60s per-command timeout.
#   FRAG_SANITIZE=asan -> -fsanitize=address,undefined -O1
set -euo pipefail

SRC=${1:?usage: build_and_link.sh <srcdir> <archive> <binary> [extra-harness-cflags...]}
ARCHIVE=${2:?}
BIN=${3:?}
shift 3 || true
EXTRA=${*:-}

ROOT=$(cd "$(dirname "$0")/.." && pwd)
GLIB_INC=$ROOT/inputs/glib-headers/usr/include/glib-2.0
GLIB_CFG=$ROOT/inputs/glib-headers/usr/lib/x86_64-linux-gnu/glib-2.0/include
LOCK=${HARDDIND_KERNEL_BUILD_LOCK:-/root/harddind-rebuild/kernel-build.lock}

if [ -z "${HARDDIND_BUILD_LOCK_FD:-}" ]; then
    exec 9>"$LOCK"
    flock -w 600 -x 9
    export HARDDIND_BUILD_LOCK_FD=9
fi

if [ "${FRAG_SANITIZE:-}" = "asan" ]; then
    SANFLAGS=(-fsanitize=address,undefined -fno-omit-frame-pointer)
    OPTFLAGS=(-O1 -g)
else
    SANFLAGS=()
    OPTFLAGS=(-O2 -g)
fi

OBJDIR=$(dirname "$ARCHIVE")/obj
mkdir -p "$OBJDIR"
rm -f "$OBJDIR"/*.o

: > "$(dirname "$BIN")/compile.log"
for c in "$SRC"/src/*.c; do
    o=$OBJDIR/$(basename "${c%.c}").o
    # Per-file wall-clock cap 60s; <=4 CPUs; sequential.
    if ! timeout --kill-after=5 60 taskset -c 0-3 \
            gcc -D_GNU_SOURCE -DG_LOG_DOMAIN='"Slirp"' \
                -I"$GLIB_INC" -I"$GLIB_CFG" -I"$SRC/src" \
                "${OPTFLAGS[@]}" "${SANFLAGS[@]}" -Wall -Wextra -fPIC -c "$c" \
                -o "$o" \
                >>"$(dirname "$BIN")/compile.log" 2>&1; then
        echo "COMPILE FAILED: $c" >&2
        exit 1
    fi
done
ar rcs "$ARCHIVE" "$OBJDIR"/*.o

timeout --kill-after=5 60 taskset -c 0-3 \
    gcc -D_GNU_SOURCE -I"$GLIB_INC" -I"$GLIB_CFG" -I"$SRC/src" \
        "${OPTFLAGS[@]}" "${SANFLAGS[@]}" -Wall -Wextra -o "$BIN" \
        "$ROOT/tests/frag_repro.c" "$ARCHIVE" -l:libglib-2.0.so.0 \
        $EXTRA -Wl,--allow-multiple-definition \
    >>"$(dirname "$BIN")/compile.log" 2>&1

echo "built $ARCHIVE ($(ls "$OBJDIR"/*.o | wc -l) objects, sanitizer=${FRAG_SANITIZE:-none}) and $BIN"
