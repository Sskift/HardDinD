#!/bin/bash
# GPU-free host build + layout/parser verification for the OS-event ABI
# probe.  One single-threaded gcc under one bounded lock acquisition
# (<=60 s flock; no parallel make, no nested lock).  No GPU is touched:
# the binary runs only in --layout-selfcheck mode; ABI semantics are
# validated through parser/model unit tests in Python.
set -eu -o pipefail

cd "$(dirname "$0")/.."
LOCK=/root/harddind-rebuild/kernel-build.lock
BIN=build/gpu-os-event-probe
mkdir -p build

for attempt in $(seq 1 20); do
    set +e
    timeout 60 flock -x "$LOCK" gcc -O2 -Wall -Wextra -Werror -std=gnu11 \
        src/gpu_os_event_probe.c -o "$BIN"
    rc=$?
    set -e
    if [ "$rc" = 0 ]; then break; fi
    if [ "$rc" != 124 ]; then echo "compile failed rc=$rc" >&2; exit 1; fi
    sleep 15
done

if [ ! -x "$BIN" ]; then echo "build skipped (lock busy)" >&2; exit 2; fi

"$BIN" --layout-selfcheck | tee build/event-layout.log \
    | python3 scripts/gpu_os_event_checks.py /dev/stdin --layout
python3 -m unittest discover -s tests -p 'test_gpu_os_event_checks.py' -v \
    | tee build/event-selftest.log
