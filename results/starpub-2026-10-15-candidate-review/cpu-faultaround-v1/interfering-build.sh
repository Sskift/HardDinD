#!/bin/bash
set -e
exec 9>/kernel-build.lock
flock -x 9
cd /root/harddind-rebuild/arkclaude/round5/gpu-mapping-repair
TREE=.work/linux-lockcycle
rm -rf "$TREE"; mkdir -p "$TREE"
tar xf .work/cache/linux-7.2.10.tar.xz -C "$TREE" --strip-components=1
cp -a .work-lockcycle/src/. "$TREE/"
cp kernel/runtime.config "$TREE/.config"
cd "$TREE"
make -j4 ARCH=um olddefconfig
make -j4 ARCH=um linux
echo BUILD_DONE
