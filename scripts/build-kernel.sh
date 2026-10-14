#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$repo/.work}
mkdir -p "$work"
exec 9>"$work/kernel-build.lock"
flock -x 9
src=$(python3 "$repo/scripts/fetch-kernel.py" "$work")
if [ "${PROTECT_EXPERIMENT:-0}" = 1 ]; then IPI_EXPERIMENT=1; fi
if [ "${IPI_EXPERIMENT:-0}" = 1 ]; then PERFORMANCE_EXPERIMENT=1; fi
if [ "${PERFORMANCE_EXPERIMENT:-0}" = 1 ]; then CACHE_EXPERIMENT=1; fi
if [ "${CACHE_EXPERIMENT:-0}" = 1 ]; then DISPATCH_EXPERIMENT=1; fi
if [ "${DISPATCH_EXPERIMENT:-0}" = 1 ]; then THREADS_EXPERIMENT=1; fi
if [ "${THREADS_EXPERIMENT:-0}" = 1 ]; then
    [ "${HANDOFF_EXPERIMENT:-0}" = 0 ] || { echo 'select only one experiment' >&2; exit 1; }
    out=$work/thread-build
else
    out=$work/kernel-build
fi
mkdir -p "$out"
out=$(realpath "$out")
options=()
if [ "${HANDOFF_EXPERIMENT:-0}" = 1 ]; then options+=(--handoff-experiment); fi
if [ "${PROTECT_EXPERIMENT:-0}" = 1 ]; then
    options+=(--protect-experiment)
elif [ "${IPI_EXPERIMENT:-0}" = 1 ]; then
    options+=(--ipi-experiment)
elif [ "${PERFORMANCE_EXPERIMENT:-0}" = 1 ]; then
    options+=(--performance-experiment)
elif [ "${CACHE_EXPERIMENT:-0}" = 1 ]; then
    options+=(--cache-experiment)
elif [ "${DISPATCH_EXPERIMENT:-0}" = 1 ]; then
    options+=(--dispatch-experiment)
elif [ "${THREADS_EXPERIMENT:-0}" = 1 ]; then
    options+=(--threads-experiment)
fi
python3 "$repo/scripts/apply-kernel-patches.py" "$src" "$work/cache/$(basename "$src").tar.xz" "${options[@]}"
# Resolve the requested config separately. Temporarily writing defconfig into
# the build tree changes autoconf headers and needlessly recompiles everything.
config_stage=$(mktemp -d "$out/.config-stage-XXXXXX")
trap 'rm -rf "$config_stage"' EXIT
make -C "$src" O="$config_stage" ARCH=um defconfig >/dev/null
KCONFIG_CONFIG="$config_stage/.config" "$src/scripts/kconfig/merge_config.sh" -m -O "$config_stage" "$config_stage/.config" "$repo/kernel/runtime.config" >/dev/null
make -C "$src" O="$config_stage" ARCH=um olddefconfig >/dev/null
python3 - "$repo/kernel/runtime.config" "$config_stage/.config" <<'PY'
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
printf '%s\n' "$out/linux"
