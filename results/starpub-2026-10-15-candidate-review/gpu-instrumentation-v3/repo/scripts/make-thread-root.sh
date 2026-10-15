#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
root=$work/thread-root
disk=$work/thread.ext4
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'thread root or disk already exists' >&2; exit 1; }
cp -a "$work/probe-root" "$root"
gcc -O2 -Wall -Wextra -Werror -static -pthread "$repo/src/thread_semantics.c" -o "$root/thread-semantics"
install -m 755 "$repo/tests/thread-init" "$root/sbin/harddind-thread-init"
truncate -s 1G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/thread-semantics"
