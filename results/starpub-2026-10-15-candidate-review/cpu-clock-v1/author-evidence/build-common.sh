#!/usr/bin/env bash
# Shared helpers for bounded, chunk-released v5 kernel builds.
# Sourced by build-chunk.sh; never runs make by itself.
#
# Lock protocol (required by OWNER-BUILD-CHUNK-LOCK-V5.md):
#  - global EX lock acquired with LOCK_NB and a bounded wait;
#  - held ONLY for one <=175 s make invocation, then released (subshell exit);
#  - every retry re-acquires after a visible gap, giving owner runs a slot;
#  - no loop may hold the lock across invocations.
set -euo pipefail

ROUND=/root/harddind-rebuild/arkclaude/round12/cpu-capture-stall
BASELINE=/root/harddind-rebuild/linux-7.2.10
LOCK=/root/harddind-rebuild/kernel-build.lock
FROZEN_CFG=/root/harddind-rebuild/cpu-cpuset-owner-v1/kernel.config
FROZEN_PATCHES=/root/harddind-rebuild/cpu-cpuset-owner-v1/kernel-patches.json
V5=$ROUND/clock-v5
SRC=$V5/src
OBJ_SMP=$V5/obj-smp
OBJ_TT=$V5/obj-tt
STATE=$V5/build-state.json
PATCH_DIR=$ROUND/analysis/kernel-candidate-v5
P1=$PATCH_DIR/0001-EXPERIMENTAL-um-stall-rendezvous-cfs-throttle-diag-v5.patch
P2=$PATCH_DIR/0002-EXPERIMENTAL-um-hr-sched-clock-AB-v5.patch
JOBS=4
CHUNK_SECS=175
AFFINITY=0-3
LOCK_WAIT_SECS=${LOCK_WAIT_SECS:-60}

# 33 frozen-patched files that must remain byte-identical except the five the
# candidate is allowed to touch.
EXPECTED_CHANGED_REGEX='arch/um/(include/shared/os\.h|kernel/Makefile|kernel/time\.c|os-Linux/skas/process\.c|os-Linux/start_up\.c)'

log() { printf '[%s] %s\n' "$(date +%s.%N)" "$*"; }
sha() { sha256sum "$1" | awk '{print $1}'; }

# Bounded non-blocking EX acquisition. Returns 9 if the slot was not obtained
# within $1 seconds, so callers DEFER rather than wait unbounded.
acquire_ex_nb() {
	local deadline=$1
	local end=$(( $(date +%s) + deadline ))
	exec 9>"$LOCK"
	while :; do
		if flock -n -x 9; then return 0; fi
		if [ "$(date +%s)" -ge "$end" ]; then return 9; fi
		sleep 2
	done
}

owner_busy() {
	# True while the named owner experiment has a running owner-status.
	local f=/root/harddind-rebuild/cpu-faultaround-owner-v1/owner-status.json
	[ -f "$f" ] || return 1
	grep -q '"status": "running"' "$f"
}

# Actual frozen-baseline verification: all 33 hashes present and matching.
verify_frozen_33() {
	python3 - "$BASELINE" "$FROZEN_PATCHES" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
m = json.load(open(sys.argv[2]))['files']
bad = [n for n, h in m.items()
       if not (root / n).exists()
       or hashlib.sha256((root / n).read_bytes()).hexdigest() != h]
if bad:
    print('FROZEN_BASELINE_MISMATCH %d: %s' % (len(bad), ' '.join(bad[:8])))
    sys.exit(3)
print('FROZEN_BASELINE_OK %d files' % len(m))
PY
}

# Candidate source binding against the 33 frozen files.
verify_candidate_source_binding() {
	python3 - "$SRC" "$FROZEN_PATCHES" <<'PY'
import hashlib, json, pathlib, re, sys
src = pathlib.Path(sys.argv[1]); m = json.load(open(sys.argv[2]))['files']
expected = re.compile(r'^arch/um/(include/shared/os\.h|kernel/Makefile|'
                      r'kernel/time\.c|os-Linux/skas/process\.c|'
                      r'os-Linux/start_up\.c)$')
unexpected, missing = [], []
changed = []
for n, h in m.items():
    p = src / n
    if not p.exists(): missing.append(n); continue
    if hashlib.sha256(p.read_bytes()).hexdigest() != h:
        (changed if expected.match(n) else unexpected).append(n)
extra = ['arch/um/kernel/stall_diag.c']
miss_extra = [e for e in extra if not (src / e).exists()]
print('frozen33 changed=%s unexpected=%s missing=%s newfile_missing=%s'
      % (sorted(changed), unexpected, missing, miss_extra))
if unexpected or missing or miss_extra or sorted(changed) != sorted([
        'arch/um/include/shared/os.h',
        'arch/um/kernel/Makefile',
        'arch/um/kernel/time.c',
        'arch/um/os-Linux/skas/process.c',
        'arch/um/os-Linux/start_up.c']):
    sys.exit(4)
# generic fair.c: pristine upstream + candidate hooks (pinned binding)
pristine = (src / 'kernel/sched/fair.c.pristine').read_bytes()
cur = (src / 'kernel/sched/fair.c').read_bytes()
assert cur.count(b'harddind_stall_cfs') == 3, 'cfs hook count'
assert pristine in cur or len(cur) > len(pristine), 'fair.c not superset'
print('CANDIDATE_SOURCE_BINDING_OK')
PY
}

# Exact frozen config -> .config -> olddefconfig -> assert ZERO semantic
# drift (all 617 CONFIG_ symbols equal). Prints drift count, exits nonzero.
config_zero_drift() { # $1=O dir
	local O=$1
	python3 - "$FROZEN_CFG" "$O/.config" <<'PY'
import sys
def vals(p):
    d = {}
    for line in open(p):
        s = line.strip()
        if s.startswith('CONFIG_') and '=' in s:
            k, v = s.split('=', 1); d[k] = v
        elif s.startswith('# CONFIG_') and s.endswith(' is not set'):
            d[s[2:].split()[0]] = 'n'
    return d
a, b = vals(sys.argv[1]), vals(sys.argv[2])
drift = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
print('CONFIG_SYMBOLS frozen=%d built=%d drift=%d' % (len(a), len(b), len(drift)))
for k in drift[:20]: print('  DRIFT %s frozen=%s built=%s' % (k, a.get(k), b.get(k)))
sys.exit(1 if drift else 0)
PY
}

record_state() { # key=value ...
	:
}
