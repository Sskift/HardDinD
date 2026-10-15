#!/usr/bin/env bash
# Build the HardDinD network backend, statically linked against the pinned
# HardDinD-patched libslirp 4.6.1 (built by build-libslirp.sh), with glib kept
# dynamic from the host runtime package.
#
# This script is the single owner of the global build lock for the whole
# pipeline; build-libslirp.sh reuses the inherited, inode-verified FD instead
# of re-opening the lock (nested flock on a fresh open file description
# self-deadlocks). Do not wrap this script in another flock.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:?real work directory required}
variant=${2:-}
LOCK=${HARDDIND_KERNEL_BUILD_LOCK:-/root/harddind-rebuild/kernel-build.lock}

if [ -n "${HARDDIND_BUILD_LOCK_FD:-}" ]; then
    fd=$HARDDIND_BUILD_LOCK_FD
    target=$(stat -L -c '%d:%i' "$LOCK")
    actual=$(stat -L -c '%d:%i' "/proc/$$/fd/$fd")
    if [ "$target" != "$actual" ]; then
        echo "inherited build lock FD $fd is $actual, expected $LOCK ($target)" >&2
        exit 78
    fi
else
    exec 9>"$LOCK"
    # Bounded wait: fail closed instead of hanging behind a stale holder.
    flock -w "${HARDDIND_LOCK_WAIT:-600}" -x 9
    export HARDDIND_BUILD_LOCK_FD=9
fi

# Subordinate build inherits the held FD and must not re-lock.
bash "$repo/scripts/build-libslirp.sh" "$work" "$variant"

libdir="$work/libslirp-harddind"
if [ "$variant" = "asan" ]; then
    out="$work/network-backend-asan"
    archive="$libdir/libslirp-asan.a"
    san=(-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer)
else
    out="$work/network-backend"
    archive="$libdir/libslirp-harddind.a"
    san=(-O2 -g)
fi
[ -f "$archive" ] || { echo "missing $archive (build-libslirp.sh failed)" >&2; exit 1; }

# Link the pinned host runtime soname directly: the extracted glib dev
# directory only provides headers and a dev symlink, and the dynamic dependency
# must be the ABI-pinned libglib-2.0.so.0 anyway.
# awk consumes the whole stream (no early exit) so pipefail cannot turn a
# producer SIGPIPE into a spurious build failure.
glib_lib=$(ldconfig -p | awk '/libglib-2.0\.so\.0 \(/{if (!seen) {print $NF; seen=1}} END{exit seen ? 0 : 1}')
[ -n "$glib_lib" ] && [ -f "$glib_lib" ] || { echo "pinned libglib-2.0.so.0 not found" >&2; exit 1; }

gcc "${san[@]}" -Wall -Wextra -Werror \
    "$repo/src/network_backend.c" "$archive" "$glib_lib" -o "$out.new"
[ "$("$out.new" --version)" = "4.6.1" ]
mv "$out.new" "$out"
sha256sum "$out"

# Native C fragment unit tests link internal slirp headers directly (virtual
# clock expiry at 2^32/3*2^32, 65535/65536 bounds, atomic-fragment overwrite,
# overlap/first/terminal policy, reserved-field acceptance).
glib_inc="$work/glib-headers/usr/include/glib-2.0"
glib_cfg="$work/glib-headers/usr/lib/x86_64-linux-gnu/glib-2.0/include"
stage_src="$work/libslirp-stage/libslirp-v4.6.1/src"
cunit="$work/native_ip6frag${variant:+-$variant}"
gcc -D_GNU_SOURCE "${san[@]}" -Wall -Wextra -Werror \
    -I"$glib_inc" -I"$glib_cfg" -I"$stage_src" \
    "$repo/tests/publish/native_ip6frag.c" "$archive" "$glib_lib" -o "$cunit"
if [ "$variant" = "asan" ]; then
    ASAN_OPTIONS=detect_leaks=0:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
        "$cunit"
else
    "$cunit"
fi
