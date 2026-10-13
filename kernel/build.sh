#!/bin/bash
# Build the udock UML kernel: ./build.sh <linux-src-dir> <out-binary>
set -euo pipefail

src=$(realpath "$1")
out=$2
frag=$(realpath "$(dirname "$0")/docker.config")

cd "$src"
# UML hardcodes HZ=100 (no Kconfig choice). Guest network round trips wait on
# ticks, so 1ms ticks raise TCP throughput through udock-net.
sed -i '/^config HZ$/,/^$/s/default 100$/default 1000/' arch/um/Kconfig
make ARCH=um defconfig >/dev/null
ARCH=um scripts/kconfig/merge_config.sh -m .config "$frag" >/dev/null
make ARCH=um olddefconfig >/dev/null

# merge_config silently drops options whose dependencies are unmet; report them.
missing=0
while IFS= read -r line; do
  [[ $line =~ ^(CONFIG_[A-Z0-9_]+)=y$ ]] || continue
  if ! grep -qx "${BASH_REMATCH[1]}=y" .config; then
    echo "not set: ${BASH_REMATCH[1]}" >&2
    missing=1
  fi
done < "$frag"

make ARCH=um -j"$(nproc)" linux >/dev/null
strip -o "$out" linux
echo "built $out ($(du -h "$out" | cut -f1))"
exit $missing
