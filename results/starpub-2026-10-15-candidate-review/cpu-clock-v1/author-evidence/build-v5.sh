#!/usr/bin/env bash
# Orchestrator for the v5 full links. It NEVER holds the EX lock itself:
# every make runs in a child build-chunk.sh that acquires the lock with a
# bounded non-blocking wait, runs ONE <=175 s invocation, and releases.
# Between chunks this driver sleeps (giving owner runs a slot) and re-checks
# whether an owner experiment is active. Stops on: hard build failure,
# bounded attempts exhausted, or owner active (DEFER, exit 9 - rerun later).
#
# Resume-safe: re-running continues from incremental objects; final artifacts
# are hashed into MANIFEST-v5.json. Builds only; no VM/GPU/perf.
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source "$here/build-common.sh"

MAX_LINK_CHUNKS=${MAX_LINK_CHUNKS:-20}
GAP_SECS=${GAP_SECS:-6}
LOCK_WAIT_SECS=${LOCK_WAIT_SECS:-60}

sha() { sha256sum "$1" | awk '{print $1}'; }

# Pre-build hashes (source + frozen config). Captured before any make.
write_pre_manifest() {
	python3 - "$SRC" "$FROZEN_CFG" "$STATE" <<'PY'
import hashlib, json, pathlib, sys
src, frozen_cfg, state = map(pathlib.Path, sys.argv[1:4])
files = [
 'arch/um/include/shared/os.h','arch/um/kernel/Makefile',
 'arch/um/kernel/stall_diag.c','arch/um/kernel/time.c',
 'arch/um/os-Linux/skas/process.c','arch/um/os-Linux/start_up.c',
 'kernel/sched/fair.c','kernel/sched/fair.c.pristine']
m = json.loads(state.read_text()) if state.exists() else {}
m['pre'] = {
 'source': {f: hashlib.sha256((src/f).read_bytes()).hexdigest() for f in files},
 'frozen_config': hashlib.sha256(pathlib.Path(frozen_cfg).read_bytes()).hexdigest(),
}
state.write_text(json.dumps(m, indent=2, sort_keys=True) + '\n')
print('pre-manifest written')
PY
}

one_chunk_loop() { # kind
	local kind=$1 n=1 rc
	while [ "$n" -le "$MAX_LINK_CHUNKS" ]; do
		if owner_busy; then
			log "owner experiment running; DEFER after $((n-1)) chunks; rerun later"
			return 9
		fi
		set +e
		out=$(taskset -c "$AFFINITY" timeout "$((LOCK_WAIT_SECS+CHUNK_SECS+15))" \
			bash "$here/build-chunk.sh" link "$kind" 2>&1)
		rc=$?
		set -e
		echo "$out"
		if [ "$rc" -eq 0 ]; then
			log "$kind linked after $n chunk(s)"
			return 0
		fi
		if [ "$rc" -ne 124 ]; then
			log "$kind HARD FAILURE rc=$rc (not retried)"
			return "$rc"
		fi
		n=$((n+1))
		log "gap ${GAP_SECS}s before next lock acquisition (owner slot)"
		sleep "$GAP_SECS"
	done
	log "exhausted $MAX_LINK_CHUNKS bounded chunks for $kind"
	return 10
}

# TT config = exact frozen config with only SMP off + TT on (and unavoidable
# kconfig deps); olddefconfig resolves deps, then the diff vs frozen is
# recorded explicitly.
prepare_tt_config() {
	mkdir -p "$OBJ_TT"
	if [ ! -f "$OBJ_TT/.config" ] || ! grep -q '^CONFIG_UML_TIME_TRAVEL_SUPPORT=y' "$OBJ_TT/.config"; then
		cp "$FROZEN_CFG" "$OBJ_TT/.config"
		"$SRC/scripts/config" --file "$OBJ_TT/.config" -d SMP -e UML_TIME_TRAVEL_SUPPORT
	fi
	if ! grep -q '^# CONFIG_SMP is not set' "$OBJ_TT/.config" || ! grep -q '^CONFIG_UML_TIME_TRAVEL_SUPPORT=y' "$OBJ_TT/.config"; then
		echo "TT config priming failed" >&2; exit 4
	fi
}

record_tt_diff() {
	python3 - "$FROZEN_CFG" "$OBJ_TT/.config" <<'PY'
import sys
def vals(p):
    d={}
    for line in open(p):
        s=line.strip()
        if s.startswith('CONFIG_') and '=' in s:
            k,v=s.split('=',1); d[k]=v
        elif s.startswith('# CONFIG_') and s.endswith(' is not set'):
            d[s[2:].split()[0]]='n'
    return d
a,b=vals(sys.argv[1]),vals(sys.argv[2])
d=sorted((k,a.get(k),b.get(k)) for k in set(a)|set(b) if a.get(k)!=b.get(k))
print('TT vs frozen semantic diffs: %d (expected: SMP off + TT on + deps)'%len(d))
for k,av,bv in d: print('  %s %s -> %s'%(k,av,bv))
PY
}

write_post_manifest() {
	python3 - "$STATE" "$OBJ_SMP" "$OBJ_TT" <<'PY'
import json, pathlib, sys
state, smp, tt = map(pathlib.Path, sys.argv[1:4])
m = json.loads(state.read_text()) if state.exists() else {}
for kind, d in (('smp', smp), ('tt', tt)):
    k = d/'linux'; cfg = d/'.config'
    m.setdefault('post', {})[kind] = {
      'kernel_exists': k.exists(),
      'kernel_sha256': __import__('hashlib').sha256(k.read_bytes()).hexdigest() if k.exists() else None,
      'kernel_size': k.stat().st_size if k.exists() else None,
      'config_sha256': __import__('hashlib').sha256(cfg.read_bytes()).hexdigest() if cfg.exists() else None,
      'link_rc': (d/'link.rc').read_text().strip() if (d/'link.rc').exists() else None,
    }
state.write_text(json.dumps(m, indent=2, sort_keys=True)+'\n')
print('post-manifest:', json.dumps(m['post'], indent=1))
PY
}

main() {
	verify_frozen_33
	verify_candidate_source_binding
	write_pre_manifest

	# exact frozen SMP config (zero drift) - one bounded make chunk
	taskset -c "$AFFINITY" bash "$here/build-chunk.sh" cfg smp
	one_chunk_loop smp
	taskset -c "$AFFINITY" bash "$here/build-chunk.sh" audit smp

	# TT config (SMP off + TT on, explicit diff)
	prepare_tt_config
	taskset -c "$AFFINITY" bash "$here/build-chunk.sh" cfg tt
	record_tt_diff
	one_chunk_loop tt
	taskset -c "$AFFINITY" bash "$here/build-chunk.sh" audit tt

	write_post_manifest
}
main "$@"
