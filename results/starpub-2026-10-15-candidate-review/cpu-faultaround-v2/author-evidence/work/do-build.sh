#!/usr/bin/env bash
# One-shot locked object build. Acquires the global exclusive lock once
# (bounded wait), pins to CPUs 0-3, and bounds each command to 170s.
set -u
BASE=/root/harddind-rebuild
ROOT=$BASE/arkclaude/round17/anonymous-fault-performance
SRC=$ROOT/work/linux
BUILD=$ROOT/work/build
LOG=$ROOT/work/build.log
exec 9>"$BASE/kernel-build.lock"
echo "waiting for kernel-build.lock ..."
if ! flock -w 180 -x 9; then echo "LOCK_TIMEOUT"; exit 10; fi
echo "lock acquired pid=$$"
taskset -pc 0-3 $$ >/dev/null
mkdir -p "$BUILD"
cp "$BASE/gpu-completion-owner-v1/kernel.config" "$BUILD/.config"
run() { echo "+ $*"; timeout --signal=KILL 170 "$@"; rc=$?; echo "rc=$rc"; }
cd "$SRC"
run make O="$BUILD" ARCH=um olddefconfig
for i in 1 2 3 4 5 6 7 8 9 10; do
	echo "build chunk $i"
	timeout --signal=KILL 170 make O="$BUILD" ARCH=um -j4
	rc=$?
	echo "chunk $i rc=$rc"
	if [ "$rc" -eq 0 ]; then echo BUILD_OK; exit 0; fi
	if [ "$rc" -ne 124 ] && [ "$rc" -ne 137 ]; then echo BUILD_FAIL; exit 1; fi
done
echo BUILD_TIMEOUT_EXHAUSTED
exit 2
