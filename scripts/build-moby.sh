#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
python3 "$repo/scripts/fetch-moby.py" "$work" > "$work/moby-inputs.json"
patch_sha=$(sha256sum "$repo/moby/patches/0001-rootful-port-driver.patch" | cut -d' ' -f1)
src="$work/moby-port-$patch_sha"
if [ ! -d "$src" ]; then
    cp -a "$work/moby-port-pristine" "$src"
    patch --batch -d "$src" -p1 < "$repo/moby/patches/0001-rootful-port-driver.patch"
fi
export PATH="$work/go-build-toolchain/bin:$PATH"
export GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOFLAGS=-mod=vendor
export GOCACHE="$work/cache/go-build" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOMAXPROCS=8
export VERSION=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["moby_port_backend"]["version"])' "$repo/sources.lock.json")
commit=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["moby_port_backend"]["commit"])' "$repo/sources.lock.json")
export DOCKER_GITCOMMIT="$commit-harddind-port-${patch_sha:0:12}"
export SOURCE_DATE_EPOCH=$(python3 -c 'import json,sys;print(int(json.load(open(sys.argv[1]))["moby"]["epoch"]))' "$work/moby-inputs.json")
[ "$(go version)" = 'go version go1.26.8 linux/amd64' ]
(cd "$src" && bash hack/make.sh binary-daemon binary-proxy)
mkdir -p "$work/moby-port-bin"
cp "$src/bundles/binary-daemon/dockerd" "$work/moby-port-bin/dockerd"
cp "$src/bundles/binary-proxy/docker-proxy" "$work/moby-port-bin/docker-proxy"
"$work/moby-port-bin/dockerd" --version
sha256sum "$work/moby-port-bin/dockerd" "$work/moby-port-bin/docker-proxy"
