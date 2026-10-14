#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
base=$(realpath "${2:-$work/protect-docker-root}")
root=$work/runtime-root
disk=$work/runtime.ext4
exec 9>"$work/kernel-build.lock"
flock -x 9
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'runtime fixture already exists' >&2; exit 1; }
cp -a "$base" "$root"
install -m 755 "$repo/guest/runtime-init" "$root/sbin/harddind-runtime-init"
install -m 755 "$repo/guest/runtime-daemon" "$root/sbin/harddind-daemon"
mkdir -p "$work/client/cli-plugins"
cp -L "$root/lib/ld-musl-x86_64.so.1" "$work/client/ld-musl-x86_64.so.1"
cp "$root/usr/bin/docker" "$work/client/docker"
cp "$root/usr/libexec/docker/cli-plugins/docker-buildx" "$work/client/buildx"
python3 - "$work/client" <<'PY'
import pathlib,shlex,sys
p=pathlib.Path(sys.argv[1])
f=p/'cli-plugins/docker-buildx'
f.write_text('#!/bin/sh\nexec '+shlex.quote(str(p/'ld-musl-x86_64.so.1'))+' '+shlex.quote(str(p/'buildx'))+' "$@"\n')
f.chmod(0o755)
PY
truncate -s 4G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/sbin/harddind-runtime-init" "$root/sbin/harddind-daemon" "$work/client/docker"
