#!/usr/bin/env bash
# Build the HardDinD network backend, statically linked against the pinned
# HardDinD-patched libslirp 4.6.1 and the pinned glib development archive.
#
# Locking is owned by the pipeline (./hd build); this script takes none.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:?real work directory required}
variant=${2:-}

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

glib_lib="$work/glib-headers/usr/lib/x86_64-linux-gnu/libglib-2.0.a"
stage_src="$work/libslirp-stage/libslirp-v4.6.1/src"
link=(-static -pthread -lm)
# ASan needs a dynamic libc; production builds remain static.
if [ "$variant" = "asan" ]; then link=(-pthread -lm); fi
[ -f "$glib_lib" ] || { echo "pinned glib archive not found" >&2; exit 1; }

gcc "${san[@]}" -Wall -Wextra -Werror \
    -I"$stage_src" "$repo/src/network_backend.c" "$archive" "$glib_lib" "${link[@]}" \
    -Wl,--wrap=send,--wrap=recv -o "$out.new"
[ "$("$out.new" --version)" = "4.6.1" ]
mv "$out.new" "$out"
sha256sum "$out"

# Keep the standalone static probe available to runtime and published-port
# checks on a fresh work directory, as the previous network target did.
gcc -O2 -Wall -Wextra -Werror -static "$repo/src/network_probe.c" -o "$work/network-probe.new"
mv "$work/network-probe.new" "$work/network-probe"

# Native C fragment unit tests link internal slirp headers directly (virtual
# clock expiry at 2^32/3*2^32, 65535/65536 bounds, atomic-fragment overwrite,
# overlap/first/terminal policy, reserved-field acceptance).
glib_inc="$work/glib-headers/usr/include/glib-2.0"
glib_cfg="$work/glib-headers/usr/lib/x86_64-linux-gnu/glib-2.0/include"
stage_src="$work/libslirp-stage/libslirp-v4.6.1/src"
cunit="$work/native_ip6frag${variant:+-$variant}"
gcc -D_GNU_SOURCE "${san[@]}" -Wall -Wextra -Werror \
    -I"$glib_inc" -I"$glib_cfg" -I"$stage_src" \
    "$repo/tests/publish/native_ip6frag.c" "$archive" "$glib_lib" "${link[@]}" -o "$cunit"
if [ "$variant" = "asan" ]; then
    ASAN_OPTIONS=detect_leaks=0:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
        "$cunit"
else
    "$cunit"
fi
