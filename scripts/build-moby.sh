#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
jobs=${JOBS:-4}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be a positive integer' >&2; exit 1; }
python3 "$repo/scripts/fetch-moby.py" "$work" > "$work/moby-inputs.json"
patch_sha=$(cat "$repo"/moby/patches/[0-9]*.patch | sha256sum | cut -d' ' -f1)
src="$work/moby-port-$patch_sha"
if [ ! -d "$src" ]; then
    cp -a "$work/moby-port-pristine" "$src"
    for patch_file in "$repo"/moby/patches/[0-9]*.patch; do
        patch --batch --fuzz=0 -d "$src" -p1 < "$patch_file"
    done
fi
export PATH="$work/go-build-toolchain/bin:$PATH"
export GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOFLAGS="-mod=vendor -p=$jobs"
export GOCACHE="$work/cache/go-build" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOMAXPROCS="$jobs"
export VERSION=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["moby_port_backend"]["version"])' "$repo/sources.lock.json")
commit=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["moby_port_backend"]["commit"])' "$repo/sources.lock.json")
export DOCKER_GITCOMMIT="$commit-harddind-port-${patch_sha:0:12}"
export SOURCE_DATE_EPOCH=$(python3 -c 'import json,sys;print(int(json.load(open(sys.argv[1]))["moby"]["epoch"]))' "$work/moby-inputs.json")
[ "$(go version)" = 'go version go1.26.8 linux/amd64' ]
(cd "$src" && go test ./daemon/libnetwork/internal/rlkclient ./daemon/libnetwork/portmappers/nat -run 'TestHardDinD|TestMapPorts')
(cd "$src" && bash hack/make.sh binary-daemon binary-proxy)
mkdir -p "$work/moby-port-bin"
cp "$src/bundles/binary-daemon/dockerd" "$work/moby-port-bin/dockerd"
cp "$src/bundles/binary-proxy/docker-proxy" "$work/moby-port-bin/docker-proxy"
"$work/moby-port-bin/dockerd" --version
sha256sum "$work/moby-port-bin/dockerd" "$work/moby-port-bin/docker-proxy"
