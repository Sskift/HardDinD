#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
python3 "$repo/scripts/fetch-moby.py" "$work" > "$work/moby-inputs.json"
export PATH="$work/go-build-toolchain/bin:$PATH"
export GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GO111MODULE=off GOFLAGS=''
export GOCACHE="$work/cache/go-build" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOMAXPROCS=8
[ "$(go version)" = 'go version go1.26.8 linux/amd64' ]
go build -trimpath -buildvcs=false -o "$work/port-agent.new" "$repo/src/port_agent.go"
mv "$work/port-agent.new" "$work/port-agent"
sha256sum "$work/port-agent"
