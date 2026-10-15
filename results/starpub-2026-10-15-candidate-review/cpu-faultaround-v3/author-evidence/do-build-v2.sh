#!/usr/bin/env bash
# v2 locked build. The global EXCLUSIVE lock is acquired PER COMMAND and
# released between chunks, so other owner work can proceed. Bounded wait
# (180 s), bounded command duration (170 s), CPUs 0-3 only.
set -u
BASE=/root/harddind-rebuild
ROOT=$BASE/arkclaude/round17/anonymous-fault-performance/v2
SRC=$ROOT/work/linux
BUILD=$ROOT/work/build
LOCK=$BASE/kernel-build.lock
CFG=$BASE/gpu-completion-owner-v1/kernel.config

taskset -pc 0-3 $$ >/dev/null
mkdir -p "$BUILD"
sha256sum "$CFG" | tee "$BUILD/config.input.sha256"
cp "$CFG" "$BUILD/.config"

locked() {
	# $@ = command; flock -w 180 around exactly this one command
	(
		exec 9>"$LOCK"
		if ! flock -w 180 9; then echo "LOCK_TIMEOUT"; exit 10; fi
		taskset -pc 0-3 $$ >/dev/null
		timeout --signal=KILL 170 "$@"
	)
}

echo "== olddefconfig =="
locked make -C "$SRC" O="$BUILD" ARCH=um olddefconfig || { echo CFG_FAIL; exit 1; }
cmp -s "$BUILD/.config" "$CFG" && echo CONFIG_BYTE_IDENTICAL || {
	echo CONFIG_CHANGED; diff "$CFG" "$BUILD/.config" | head; exit 1; }

for i in 1 2 3 4 5 6 7 8 9 10; do
	echo "== build chunk $i (lock re-acquired) =="
	locked make -C "$SRC" O="$BUILD" ARCH=um -j4
	rc=$?
	echo "chunk $i rc=$rc"
	if [ "$rc" -eq 0 ]; then echo BUILD_OK; exit 0; fi
	if [ "$rc" -ne 124 ] && [ "$rc" -ne 137 ]; then echo BUILD_FAIL; exit 1; fi
done
echo BUILD_TIMEOUT_EXHAUSTED
exit 2
