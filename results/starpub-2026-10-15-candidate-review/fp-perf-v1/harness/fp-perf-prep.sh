#!/usr/bin/env bash
# fp-perf-prep.sh - build the frozen benchmark binaries and the perf guest
# disk for the owner-run descriptive perf supervisor.
#
#   fp-perf-prep.sh <work-dir> [run-timeout-seconds]
#
# Read-only inputs (hash-verified, never modified):
#   /root/harddind-rebuild/fp-owner-review-v4/src/corebench.c
#   /root/harddind-rebuild/fp-owner-review-v4/src/dispatchbench.c
#   /root/harddind-rebuild/fp-matrix-owner-v1/work/fp-root   (Alpine root)
# Outputs (refuses to overwrite):
#   <work>/bin/{corebench,dispatchbench,perfrun}
#   <work>/perf-root, <work>/perf.ext4, <work>/perf-manifest.json
#
# This is preparation only: it runs no benchmark and boots no VM. It is meant
# to be called under the global kernel-build EX lock (the supervisor does so).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(realpath "${1:?work directory required}")
RUN_TIMEOUT=${2:-45}

SRC=/root/harddind-rebuild/fp-owner-review-v4/src
BASE_ROOT=/root/harddind-rebuild/fp-matrix-owner-v1/work/fp-root
CORE_SHA=991be1c676c293e5cdc8900e0ba23fab23fb3de2a65bb19702b5472069c23a35
DISP_SHA=84d6dd6c0e154b8291bce9d69de4cac8e359fe70c5816a77b5c7d9a043c8eba7

BIN="$WORK/bin"
ROOT="$WORK/perf-root"
DISK="$WORK/perf.ext4"
MAN="$WORK/perf-manifest.json"

[ -d "$BASE_ROOT" ] || { echo "missing frozen guest root $BASE_ROOT" >&2; exit 1; }
[ ! -e "$DISK" ] || { echo "$DISK already exists; refusing to overwrite" >&2; exit 1; }

sha_check() {
	local file=$1 want=$2
	local got
	got=$(sha256sum "$file" | cut -d' ' -f1)
	[ "$got" = "$want" ] || {
		echo "hash mismatch $file: got $got want $want" >&2; exit 1; }
}

mkdir -p "$BIN"
sha_check "$SRC/corebench.c" "$CORE_SHA"
sha_check "$SRC/dispatchbench.c" "$DISP_SHA"

# Exact flags used by the frozen make-probe-root.sh for corebench; the same
# strict set is applied to dispatchbench and the perfrun harness helper.
CFLAGS=(-O2 -Wall -Wextra -Werror -static -pthread)
gcc "${CFLAGS[@]}" "$SRC/corebench.c"     -o "$BIN/corebench"
gcc "${CFLAGS[@]}" "$SRC/dispatchbench.c" -o "$BIN/dispatchbench"
gcc -O2 -Wall -Wextra -Werror -static "$HERE/perfrun.c" -o "$BIN/perfrun"

# Plumbing sanity only: no arguments must exit 2 (usage). This runs nothing.
"$BIN/perfrun" >/dev/null 2>&1 && { echo "perfrun usage check failed" >&2; exit 1; } || true

[ ! -e "$ROOT" ] && [ -L "$ROOT" ] && rm -f "$ROOT"
[ ! -e "$ROOT" ] || { echo "$ROOT already exists; refusing to overwrite" >&2; exit 1; }
# BASE_ROOT may be a symlink to a shared tree; copy CONTENTS into a real
# directory so every install below stays inside our work root.
mkdir "$ROOT"
cp -a "$BASE_ROOT"/. "$ROOT"/
install -m 755 "$BIN/corebench"     "$ROOT/corebench"
install -m 755 "$BIN/dispatchbench" "$ROOT/dispatchbench"
install -m 755 "$BIN/perfrun"       "$ROOT/sbin/perfrun"
sed "s/@RUN_TIMEOUT@/${RUN_TIMEOUT}/g" "$HERE/harddind-perf-init" \
	> "$ROOT/sbin/harddind-perf-init"
chmod 755 "$ROOT/sbin/harddind-perf-init"

truncate -s 1G "$DISK"
mkfs.ext4 -q -F -d "$ROOT" "$DISK"

python3 - "$WORK" "$RUN_TIMEOUT" <<'EOF'
import hashlib, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
here = pathlib.Path(__file__).resolve().parent
cleanup_lib = pathlib.Path(
    "/root/harddind-rebuild/fp-matrix-owner-v1/repo/scripts/guest_cleanup.py")
harness = {n: sha(here / n) for n in (
    "fp-perf-supervisor.py", "perf_log.py", "harddind-perf-init",
    "perfrun.c", "test_perf_log.py", "fp-perf-prep.sh")}
harness["repo:guest_cleanup.py"] = sha(cleanup_lib)
man = {
    "run_timeout": int(sys.argv[2]),
    "sources": {
        "corebench.c":
            "991be1c676c293e5cdc8900e0ba23fab23fb3de2a65bb19702b5472069c23a35",
        "dispatchbench.c":
            "84d6dd6c0e154b8291bce9d69de4cac8e359fe70c5816a77b5c7d9a043c8eba7",
    },
    "binaries": {n: sha(work / "bin" / n)
                 for n in ("corebench", "dispatchbench", "perfrun")},
    "harness": harness,
    "disk": sha(work / "perf.ext4"),
}
(work / "perf-manifest.json").write_text(json.dumps(man, indent=2) + "\n")
print(json.dumps(man, indent=2))
EOF
echo "$DISK"
