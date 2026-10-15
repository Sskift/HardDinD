#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
root=$work/dispatch-root
disk=$work/dispatch.ext4
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'dispatch root or disk already exists' >&2; exit 1; }
cp -a "$work/thread-root" "$root"
gcc -O2 -Wall -Wextra -Werror -static -pthread "$repo/src/dispatchbench.c" -o "$root/dispatchbench"
install -m 755 "$repo/tests/dispatch-init" "$root/sbin/harddind-dispatch-init"
truncate -s 1G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/dispatchbench"
