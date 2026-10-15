#!/usr/bin/env bash
# Build an isolated test disk; never edit a live disk.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
root=$work/probe-root
disk=$work/probe.ext4
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'probe root or disk already exists' >&2; exit 1; }
mkdir -p "$root"
if [ "$#" -ge 2 ]; then
    base=$(realpath "$2")
    cp -a "$base/." "$root/"
else
    archive=$(python3 "$repo/scripts/fetch-artifact.py" alpine "$work/cache")
    tar -xf "$archive" -C "$root"
fi
gcc -O2 -Wall -Wextra -Werror -static -pthread "$repo/src/corebench.c" -o "$root/corebench"
install -m 755 "$repo/tests/guest-init" "$root/sbin/harddind-init"
mkdir -p "$root"/{proc,sys,dev,run,tmp,mnt}
truncate -s 1G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/corebench"
printf '%s\n' "$disk"
