#!/usr/bin/env bash
# Bounded build/test wrapper: every cargo subprocess runs under GNU timeout with
# a kill-after so a hung unit test (e.g. a self-deadlocked mutex) can never hold
# the global build lock indefinitely. Max 4 jobs.
set -euo pipefail
cd /root/harddind-rebuild/arkclaude/round4/shared-posix-locks
exec 9>/root/harddind-rebuild/kernel-build.lock
flock -x 9
export PATH="$PWD/work/toolchain/bin:$PATH"
export RUSTC="$PWD/work/toolchain/bin/rustc"
export CARGO_HOME="$PWD/work/cargo-home"
export CARGO_TARGET_DIR="$PWD/work/target"
export CARGO_BUILD_JOBS=4
export CARGO_NET_OFFLINE=true
export PKG_CONFIG_SYSROOT_DIR="$PWD/work/virtiofs-native"
export PKG_CONFIG_LIBDIR="$PWD/work/virtiofs-native/usr/lib/x86_64-linux-gnu/pkgconfig"
export RUSTFLAGS="-Lnative=$PWD/work/virtiofs-native/usr/lib/x86_64-linux-gnu -Lnative=$PWD/work/virtiofs-native/lib/x86_64-linux-gnu -C link-arg=-Wl,-rpath,$PWD/work/virtiofs-native/usr/lib/x86_64-linux-gnu -C link-arg=-Wl,-rpath,$PWD/work/virtiofs-native/lib/x86_64-linux-gnu"
cd work/build/src
CARGO="$PWD/../../../work/toolchain/bin/cargo"
# Filtered harddind_ suite must finish well within 120s; 30s kill-after.
timeout --kill-after=30s 180s "$CARGO" test --release --locked --offline harddind_ -- --test-threads=1
timeout --kill-after=30s 600s "$CARGO" build --release --locked --offline
