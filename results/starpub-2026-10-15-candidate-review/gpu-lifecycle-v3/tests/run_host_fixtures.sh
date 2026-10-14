#!/bin/bash
# Build and run the executable, GPU-free negative-path host fixture for the
# CUDA lifecycle probe.  Single compiler process (no parallel make), each
# lock acquisition bounded to 60 s per the round rules.  Makes no CUDA
# calls: the probe TU is included with main renamed and only its pure host
# helpers are invoked.
set -eu -o pipefail

cd "$(dirname "$0")/.."
LOCK=/root/harddind-rebuild/kernel-build.lock
BIN=build/cuda-lifecycle-host-fixture
LOG=build/host-fixture.log
mkdir -p build

CC='gcc -O2 -g -Wall -Wextra -Werror -std=gnu11 -fsanitize=address,undefined -fno-sanitize-recover=all'

compiled=0
for attempt in $(seq 1 20); do
    set +e
    timeout 60 flock -x "$LOCK" bash -c "
        $CC -pthread tests/cuda_lifecycle_host_fixture.c -lm -ldl -o '$BIN'
    "
    rc=$?
    set -e
    if [ "$rc" = 0 ]; then
        compiled=1
        break
    fi
    if [ "$rc" != 124 ]; then
        echo "fixture compile failed rc=$rc" >&2
        exit 1
    fi
    sleep 15
done

if [ ! -x "$BIN" ]; then
    echo "fixture binary unavailable (lock busy)" >&2
    exit 2
fi

# ASan+UBSan abort non-zero on any undefined behavior or leak.
"$BIN" | tee "$LOG"
