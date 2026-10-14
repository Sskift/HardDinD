#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
root=$work/mapping-root
disk=$work/mapping.ext4
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'mapping fixture already exists' >&2; exit 1; }
cp -a "$work/dispatch-root" "$root"
gcc -O2 -Wall -Wextra -Werror -static "$repo/src/mapping_semantics.c" -o "$root/mapping-semantics"
install -m 755 "$repo/tests/mapping-init" "$root/sbin/harddind-mapping-init"
truncate -s 1G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/mapping-semantics"
