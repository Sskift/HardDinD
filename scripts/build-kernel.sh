#!/usr/bin/env bash
# Build the UML kernel from the pinned source and an explicit patch list.
# Usage: build-kernel.sh WORK OUTPUT PATCH...
# Locking and caching are owned by the pipeline (./hd build).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
output=${2:?output path required}
shift 2
src=$(python3 "$repo/scripts/fetch-kernel.py" "$work")
python3 "$repo/pipeline/patches.py" "$src" "$work/cache/$(basename "$src").tar.xz" "$@"
out="$work/kernel-build"
mkdir -p "$out"
# Resolve the requested config separately. Temporarily writing defconfig into
# the build tree changes autoconf headers and needlessly recompiles everything.
config_stage=$(mktemp -d "$out/.config-stage-XXXXXX")
trap 'rm -rf "$config_stage"' EXIT
make -C "$src" O="$config_stage" ARCH=um defconfig >/dev/null
KCONFIG_CONFIG="$config_stage/.config" "$src/scripts/kconfig/merge_config.sh" -m -O "$config_stage" "$config_stage/.config" "$repo/kernel/config" >/dev/null
make -C "$src" O="$config_stage" ARCH=um olddefconfig >/dev/null
python3 - "$repo/kernel/config" "$config_stage/.config" <<'PY'
import pathlib, sys
requested = pathlib.Path(sys.argv[1]).read_text().splitlines()
actual = set(pathlib.Path(sys.argv[2]).read_text().splitlines())
missing = [s for s in requested if s.startswith('CONFIG_') and s not in actual]
if missing:
    raise SystemExit('Required kernel options unavailable:\n' + '\n'.join(missing))
PY
if ! cmp -s "$config_stage/.config" "$out/.config"; then
    cp "$config_stage/.config" "$out/.config"
fi
make -C "$src" O="$out" ARCH=um -j"${JOBS:-8}" linux
python3 "$repo/scripts/check-stub-relocations.py" "$out/arch/um/kernel/skas/stub.o"
install -m 755 "$out/linux" "$output"
