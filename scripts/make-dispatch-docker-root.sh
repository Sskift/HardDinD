#!/usr/bin/env bash
# Extend a pristine, pinned Docker fixture without rebuilding its packages.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
base=$(realpath "${2:-$work/docker-root}")
name=${3:-dispatch-docker}
[[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || { echo 'invalid fixture name' >&2; exit 1; }
root=$work/$name-root
disk=$work/$name.ext4
exec 9>"$work/kernel-build.lock"
flock -x 9
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'dispatch Docker root or disk already exists' >&2; exit 1; }
cp -a "$base" "$root"
install -m 755 "$work/dispatch-root/dispatchbench" "$root/dispatchbench"
install -m 755 "$work/dispatch-root/thread-semantics" "$root/thread-semantics"
install -m 755 "$repo/tests/docker-suite.sh" "$root/docker-suite.sh"
install -m 755 "$repo/tests/docker-init" "$root/sbin/harddind-docker-init"
truncate -s 4G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/dispatchbench" "$root/thread-semantics"
