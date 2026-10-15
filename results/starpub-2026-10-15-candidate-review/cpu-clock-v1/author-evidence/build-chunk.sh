#!/usr/bin/env bash
# One bounded build chunk per invocation. The EX lock is acquired fresh with
# a bounded non-blocking wait, held for ONE <=175 s make invocation, and
# released when this process exits. Never invoke this in a loop that retains
# the lock; the driver (build-v5.sh) calls it once per chunk with gaps.
#
# Usage:
#   build-chunk.sh cfg        # olddefconfig + zero-drift assert (smp|tt)
#   build-chunk.sh link smp   # one bounded `make linux` (rc 124 = timed out,
#                             # safe to invoke again; other nonzero = fail)
#   build-chunk.sh audit smp  # nm symbol binding + kernel hash, no build
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source "$here/build-common.sh"

phase=${1:?phase: cfg|link|audit}
kind=${2:-smp}
case "$kind" in smp) O=$OBJ_SMP ;; tt) O=$OBJ_TT ;; *) echo "bad kind" >&2; exit 2;; esac
mkdir -p "$O"

cfg_phase() {
	# Seed the config: exact frozen for SMP; frozen+SMPoff/TTon for TT
	# (caller must have applied the two symbol changes; we re-apply here to
	# be deterministic). One bounded olddefconfig, lock released after.
	if [ "$kind" = smp ]; then
		cp "$FROZEN_CFG" "$O/.config"
		log "config: copied exact frozen kernel.config ($(sha "$FROZEN_CFG"))"
	else
		cp "$FROZEN_CFG" "$O/.config"
		"$SRC/scripts/config" --file "$O/.config" -d SMP -e UML_TIME_TRAVEL_SUPPORT
		log "config: frozen with SMP off + UML_TIME_TRAVEL_SUPPORT on (intentional TT diff)"
	fi
	if ! acquire_ex_nb "$LOCK_WAIT_SECS"; then
		log "DEFER: EX lock busy (owner experiment active); no build started"
		exit 9
	fi
	log "EX acquired (pid $$); olddefconfig $kind"
	local rc=0
	taskset -c "$AFFINITY" timeout "$CHUNK_SECS" \
		make -C "$SRC" O="$O" ARCH=um olddefconfig >"$O/cfg.log" 2>&1 || rc=$?
	echo "$rc" >"$O/cfg.rc"
	log "EX released; olddefconfig rc=$rc"
	[ "$rc" -eq 0 ] || { echo "CONFIG_MAKE_FAILED rc=$rc" >&2; tail -5 "$O/cfg.log" >&2; exit "$rc"; }
	if [ "$kind" = smp ]; then
		config_zero_drift "$O"        # SMP MUST equal frozen
	else
		# TT intentionally differs; only assert the two requested flips.
		grep -q '^# CONFIG_SMP is not set' "$O/.config"
		grep -q '^CONFIG_UML_TIME_TRAVEL_SUPPORT=y' "$O/.config"
		log "TT config flips present; semantic diff audited separately"
	fi
}

link_phase() {
	if owner_busy; then
		log "DEFER: owner cpu-faultaround-owner-v1 running; no build chunk taken"
		exit 9
	fi
	if ! acquire_ex_nb 60; then
		log "DEFER: EX lock busy; no build chunk taken"
		exit 9
	fi
	log "EX acquired (pid $$) for ONE $kind link chunk (<=${CHUNK_SECS}s, -j$JOBS)"
	local rc=0
	taskset -c "$AFFINITY" timeout "$CHUNK_SECS" \
		make -C "$SRC" O="$O" ARCH=um "-j$JOBS" linux >"$O/link.log" 2>&1 || rc=$?
	echo "$rc" >"$O/link.rc"
	# Exact rc semantics: 0 linked; 124 = this chunk's timeout (incremental
	# state kept, re-run later with a fresh lock); anything else = real
	# failure that must NOT be retried.
	log "EX released; $kind link chunk rc=$rc (124=timed out -> re-run, other nonzero=hard fail)"
	if [ "$rc" -eq 0 ]; then
		echo "LINK_PRESENT $O/linux $(sha256sum "$O/linux" 2>/dev/null | awk '{print $1}')"
		exit 0
	elif [ "$rc" -eq 124 ]; then
		echo "LINK_CHUNK_TIMED_OUT (resume later; $(find "$O" -name '*.o' | wc -l) objects present)"
		exit 124
	else
		echo "LINK_FAILED rc=$rc (compile/link error, not retried)" >&2
		grep -E "error:" "$O/link.log" | head -10 >&2
		exit "$rc"
	fi
}

audit_phase() {
	# No lock: reads artifacts. Binds against the pinned-kernel precedent.
	local K=$O/linux
	[ -x "$K" ] || { echo "no linked kernel: $K" >&2; exit 5; }
	if [ "$kind" = smp ]; then
		config_zero_drift "$O"    # SMP linked config MUST equal frozen
	else
		grep -q '^# CONFIG_SMP is not set' "$O/.config"
		grep -q '^CONFIG_UML_TIME_TRAVEL_SUPPORT=y' "$O/.config"
	fi
	echo "linked=$K sha256=$(sha256sum "$K" | awk '{print $1}') size=$(stat -c %s "$K")"
	# One definition each, same lowercase binding the shipped pinned kernel
	# uses for equivalent globals (seccomp_threads/jiffies -> d/b/t).
	for sym in harddind_stall_diag_ms harddind_hr_sched_clock; do
		local line
		line=$(nm "$K" | awk -v s="$sym" '$3==s || $2==s {c++} END{print c+0}')
		[ "$line" = 1 ] || { echo "expected exactly one $sym, got $line" >&2; exit 6; }
	done
	[ "$(nm "$K" | awk '$3=="sched_clock"{c++} END{print c+0}')" = 1 ] || \
		{ echo "expected exactly one sched_clock" >&2; exit 6; }
	if nm "$K" | awk '$1=="U" && $2 ~ /^harddind/ {print}' | grep -q .; then
		echo "undefined harddind symbols in link" >&2; exit 6
	fi
	# Precedent: shipped pinned kernel localizes equivalent globals the same way.
	local pinned=/root/harddind-rebuild/cpu-cpuset-kernel-v1
	local case_pre
	case_pre=$(nm "$pinned" | awk '$3=="seccomp_threads"{print $1}' | cut -c1-1)
	local case_new
	case_new=$(nm "$K" | awk '$3=="harddind_stall_diag_ms"{print $1}' | cut -c1-1)
	echo "nm type case: candidate=$case_new pinned-precedent(seccomp_threads)=$case_pre (lowercase=UML final-link localization, identical binding)"
	[ "$case_new" = "$case_pre" ] || { echo "symbol binding case differs from pinned kernel" >&2; exit 6; }
	echo "AUDIT_OK"
}

case "$phase" in
	cfg)   cfg_phase ;;
	link)  link_phase ;;
	audit) audit_phase ;;
	*) echo "unknown phase $phase" >&2; exit 2 ;;
esac
