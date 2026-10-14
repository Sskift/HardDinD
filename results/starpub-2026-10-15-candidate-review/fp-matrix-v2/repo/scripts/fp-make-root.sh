#!/usr/bin/env bash
# Build the FP regression disk on top of an existing standard probe root:
#   fp-make-root.sh <work-dir>
# Produces <work>/fp.ext4 with /fp-probe and /sbin/harddind-fp-init.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
root=$work/fp-root
disk=$work/fp.ext4
probe=$work/probe-root
[ -d "$probe" ] || { echo "missing $probe (build the standard probe root first)" >&2; exit 1; }
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'fp root/disk already exists' >&2; exit 1; }
cp -a "$probe" "$root"
gcc -O2 -Wall -Wextra -static -pthread "$repo/src/fp_probe.c" -o "$root/fp-probe"
install -m 755 "$repo/tests/fp-lazy-init" "$root/sbin/harddind-fp-init"
truncate -s 1G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/fp-probe"
printf '%s\n' "$disk"
