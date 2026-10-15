#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$repo/.work}
artifact=$(python3 "$repo/scripts/fetch-artifact.py" docker_compose "$work/cache")
mkdir -p "$work/client/cli-plugins"
target=$work/client/cli-plugins/docker-compose
if [ -e "$target" ]; then
    cmp -s "$artifact" "$target" || { echo refusing to replace a different Compose client >&2; exit 1; }
else
    install -m 0755 "$artifact" "$target"
fi
"$target" version --short
