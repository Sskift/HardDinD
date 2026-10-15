#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
root=$work/profile-root
disk=$work/profile.ext4
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'profile root or disk already exists' >&2; exit 1; }
cp -a "$work/dispatch-root" "$root"
gcc -O2 -Wall -Wextra -Werror -static "$repo/src/profilebench.c" -o "$root/profilebench"
install -m 755 "$repo/tests/profile-init" "$root/sbin/harddind-profile-init"
truncate -s 1G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/profilebench"
