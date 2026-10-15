#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
ref=${NV_550_REF:-/root/harddind-rebuild/arkclaude/reference/nvidia-550.120}
python3 - "$repo/sources.lock.json" "$ref" <<'VERIFY'
import hashlib, json, pathlib, sys
reference = json.loads(pathlib.Path(sys.argv[1]).read_text())["nvidia_abi_reference"]
root = pathlib.Path(sys.argv[2])
for name, expected in reference["fixed_alloc_abi_headers"].items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
        raise SystemExit("NVIDIA reference differs from pinned " + reference["commit"] + ": " + name)
VERIFY
out=${1:-"$repo/.work/gpu-alloc-abi-check"}
mkdir -p "$(dirname "$out")"
gcc -O2 -Wall -Wextra -Werror -pthread \
    -I"$ref/src/common/sdk/nvidia/inc" -I"$ref/kernel-open/common/inc" \
    "$repo/src/gpu_alloc_abi_check.c" -o "$out"
"$out"
echo "GPU_ALLOC_ABI_CHECK ok"
