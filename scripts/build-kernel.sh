#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$repo/.work}
src=$(python3 "$repo/scripts/fetch-kernel.py" "$work")
out=$work/kernel-build
mkdir -p "$out"
out=$(realpath "$out")
for patchfile in "$repo"/kernel/patches/*.patch; do
    [ -f "$patchfile" ] || continue
    if patch -d "$src" -p1 --dry-run --forward < "$patchfile" >/dev/null 2>&1; then
        patch -d "$src" -p1 --forward < "$patchfile"
    else
        patch -d "$src" -p1 --dry-run --reverse < "$patchfile" >/dev/null
    fi
done
make -C "$src" O="$out" ARCH=um defconfig >/dev/null
KCONFIG_CONFIG="$out/.config" "$src/scripts/kconfig/merge_config.sh" -m -O "$out" "$out/.config" "$repo/kernel/runtime.config" >/dev/null
make -C "$src" O="$out" ARCH=um olddefconfig >/dev/null
python3 - "$repo/kernel/runtime.config" "$out/.config" <<'PY'
import pathlib, sys
requested = pathlib.Path(sys.argv[1]).read_text().splitlines()
actual = set(pathlib.Path(sys.argv[2]).read_text().splitlines())
missing = [s for s in requested if s.startswith('CONFIG_') and s not in actual]
if missing:
    raise SystemExit('Required kernel options unavailable:\n' + '\n'.join(missing))
PY
make -C "$src" O="$out" ARCH=um -j"${JOBS:-8}" linux
printf '%s\n' "$out/linux"
