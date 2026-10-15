#!/usr/bin/env bash
# Compile a frozen libslirp tree into a static archive and link the v2
# behavioral harness. NEVER acquires the lock itself: run_group.sh holds the
# single per-group EX lock and passes its FD (verified by inode).
#   FRAG_SANITIZE=asan -> -fsanitize=address,undefined -O1
set -euo pipefail

SRC=${1:?usage: build_v2.sh <srcdir> <archive> <binary> [extra cflags...]}
ARCHIVE=${2:?}
BIN=${3:?}
shift 3 || true
EXTRA=${*:-}

V2=$(cd "$(dirname "$0")/.." && pwd)
GLIB_INC=$V2/inputs/glib-headers/usr/include/glib-2.0
GLIB_CFG=$V2/inputs/glib-headers/usr/lib/x86_64-linux-gnu/glib-2.0/include
LOCK=${HARDDIND_KERNEL_BUILD_LOCK:-/root/harddind-rebuild/kernel-build.lock}

# Reuse the inherited group lock FD; never re-lock (no nested flock).
: "${HARDDIND_BUILD_LOCK_FD:?run_group.sh must export the held lock FD}"
target=$(stat -L -c '%d:%i' "$LOCK")
actual=$(stat -L -c '%d:%i' "/proc/$$/fd/$HARDDIND_BUILD_LOCK_FD")
if [ "$target" != "$actual" ]; then
    echo "inherited lock FD $HARDDIND_BUILD_LOCK_FD is $actual, expected $LOCK ($target)" >&2
    exit 78
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
# Objects are keyed by (frozen src tree, outdir-per-variant): reuse up-to-date
# objects across harness-only changes; source edits change their mtimes.

: > "$(dirname "$BIN")/compile.log"
for c in "$SRC"/src/*.c; do
    o=$OBJDIR/$(basename "${c%.c}").o
    if [ -f "$o" ] && [ "$o" -nt "$c" ]; then
        continue # up-to-date object from the same frozen source
    fi
    if ! timeout --kill-after=5 60 taskset -c 0-3 \
            gcc -D_GNU_SOURCE -DG_LOG_DOMAIN='"Slirp"' \
                -I"$GLIB_INC" -I"$GLIB_CFG" -I"$SRC/src" \
                "${OPTFLAGS[@]}" "${SANFLAGS[@]}" -Wall -Wextra -fPIC -c "$c" \
                -o "$o" >>"$(dirname "$BIN")/compile.log" 2>&1; then
        echo "COMPILE FAILED: $c" >&2
        exit 1
    fi
done
ar rcs "$ARCHIVE" "$OBJDIR"/*.o

timeout --kill-after=5 60 taskset -c 0-3 \
    gcc -D_GNU_SOURCE -I"$GLIB_INC" -I"$GLIB_CFG" -I"$SRC/src" \
        "${OPTFLAGS[@]}" "${SANFLAGS[@]}" -Wall -Wextra -o "$BIN" \
        "$V2/tests/frag_repro_v2.c" "$ARCHIVE" -l:libglib-2.0.so.0 \
        $EXTRA -Wl,--allow-multiple-definition \
    >>"$(dirname "$BIN")/compile.log" 2>&1

echo "built $ARCHIVE ($(ls "$OBJDIR"/*.o | wc -l) objects, sanitizer=${FRAG_SANITIZE:-none}) and $BIN"
