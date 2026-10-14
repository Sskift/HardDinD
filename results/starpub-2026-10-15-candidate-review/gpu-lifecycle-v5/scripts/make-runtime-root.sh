#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
base=$(realpath "${2:-$work/protect-docker-root}")
name=${3:-runtime}
ports=${4:-}
[ -z "$ports" ] || [ "$ports" = '--with-ports' ] || { echo 'unknown fixture option' >&2; exit 1; }
[[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || { echo 'invalid fixture name' >&2; exit 1; }
root=$work/$name-root
disk=$work/$name.ext4
exec 9>"$work/kernel-build.lock"
flock -x 9
[ ! -e "$root" ] && [ ! -e "$disk" ] || { echo 'runtime fixture already exists' >&2; exit 1; }
cp -a "$base" "$root"
install -m 755 "$repo/guest/runtime-init" "$root/sbin/harddind-runtime-init"
install -m 755 "$repo/guest/runtime-daemon" "$root/sbin/harddind-daemon"
if [ "$ports" = '--with-ports' ]; then
    install -m 755 "$work/port-agent" "$root/sbin/harddind-port-agent"
    install -m 755 "$work/moby-port-bin/dockerd" "$root/usr/bin/dockerd"
    install -m 755 "$work/moby-port-bin/docker-proxy" "$root/usr/bin/docker-proxy"
fi
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
if [ "$ports" = '--with-ports' ]; then
    sha256sum "$root/sbin/harddind-port-agent" "$root/usr/bin/dockerd" "$root/usr/bin/docker-proxy"
fi
