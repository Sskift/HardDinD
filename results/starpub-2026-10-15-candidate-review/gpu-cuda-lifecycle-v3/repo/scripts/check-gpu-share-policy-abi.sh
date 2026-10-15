#!/usr/bin/env bash
# Static ABI check for experiment 0023 against the pinned NVIDIA 550.120
# SDK headers. No GPU access, no kernel build.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
ref=${NV_550_REF:-/root/harddind-rebuild/arkclaude/reference/nvidia-550.120}
python3 - "$repo/sources.lock.json" "$ref" <<'VERIFY'
import hashlib, json, pathlib, sys
reference = json.loads(pathlib.Path(sys.argv[1]).read_text())["nvidia_abi_reference"]
root = pathlib.Path(sys.argv[2])
for name, expected in reference["share_policy_abi_headers"].items():
    actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("NVIDIA reference header differs from pinned " + reference["commit"] + ": " + name)
VERIFY
sdk="$ref/src/common/sdk/nvidia/inc"
openinc="$ref/kernel-open/common/inc"
out=${1:-"$repo/work/build/gpu-share-policy-abi-check"}
[ -d "$sdk" ] || { echo "missing $sdk (set NV_550_REF)" >&2; exit 2; }
mkdir -p "$(dirname "$out")"
gcc -O2 -Wall -Wextra -Werror -I"$sdk" -I"$openinc" \
    "$repo/src/gpu_share_policy_abi_check.c" -o "$out"
"$out"
echo "GPU_SHARE_ABI_CHECK ok"
