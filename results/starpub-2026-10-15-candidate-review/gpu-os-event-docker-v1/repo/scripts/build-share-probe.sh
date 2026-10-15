#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
gcc -O2 -Wall -Wextra -Werror -static "$repo/src/share_probe.c" -o "$work/share-probe.new"
mv "$work/share-probe.new" "$work/share-probe"
sha256sum "$work/share-probe"
