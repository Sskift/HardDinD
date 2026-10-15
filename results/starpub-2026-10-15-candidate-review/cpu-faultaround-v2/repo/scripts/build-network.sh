#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
# The public libslirp API does not need GLib headers; dynamic linkage resolves
# its implementation dependency without requiring the GLib development package.
gcc -O2 -Wall -Wextra -Werror "$repo/src/network_backend.c" -lslirp -o "$work/network-backend.new"
[ "$("$work/network-backend.new" --version)" = 4.6.1 ]
mv "$work/network-backend.new" "$work/network-backend"
gcc -O2 -Wall -Wextra -Werror -static "$repo/src/network_probe.c" -o "$work/network-probe"
sha256sum "$work/network-backend"
