#!/usr/bin/env bash
set -euo pipefail
exec 9>/root/harddind-rebuild/kernel-build.lock
flock -x 9
SRC=/root/harddind-rebuild/arkclaude/round5/gpu-mapping-repair/.work/linux-7.2.10
OUT=/root/harddind-rebuild/arkclaude/round5/gpu-mapping-repair/.work/build
REPO=/root/harddind-rebuild/arkclaude/round5/gpu-mapping-repair
stage=$(mktemp -d "$OUT/.cfg-XXXX")
trap 'rm -rf "$stage"' EXIT
make -C "$SRC" O="$stage" ARCH=um defconfig >/dev/null
KCONFIG_CONFIG="$stage/.config" "$SRC/scripts/kconfig/merge_config.sh" -m -O "$stage" "$stage/.config" "$REPO/kernel/runtime.config" >/dev/null
make -C "$SRC" O="$stage" ARCH=um olddefconfig >/dev/null
cmp -s "$stage/.config" "$OUT/.config" || cp "$stage/.config" "$OUT/.config"
make -C "$SRC" O="$OUT" ARCH=um -j4 linux
