#!/bin/bash
# Build and run the GPU-free negative-path host fixtures for the CUDA
# lifecycle probe.  Two single-file C compiles (sequential, no parallel
# make) under one bounded lock acquisition; each flock attempt <= 60 s.
# The probe TU is included with main renamed, so no CUDA call is made and
# no GPU/driver is needed.  ASan+UBSan abort non-zero on any UB.
set -eu -o pipefail

cd "$(dirname "$0")/.."
LOCK=/root/harddind-rebuild/kernel-build.lock
FIXBIN=build/cuda-lifecycle-host-fixture
PROTOBIN=build/owner-protocol-regression
LOG=build/host-fixture.log
PROTOLOG=build/protocol-regression.log
mkdir -p build

CC='gcc -O2 -g -Wall -Wextra -Werror -std=gnu11 -fsanitize=address,undefined -fno-sanitize-recover=all'

compiled=0
for attempt in $(seq 1 20); do
    set +e
    timeout 60 flock -x "$LOCK" bash -c "
        $CC -pthread tests/cuda_lifecycle_host_fixture.c -lm -ldl -o '$FIXBIN' && \
        $CC -pthread tests/owner_protocol_regression.c -lm -ldl -o '$PROTOBIN'
    "
    rc=$?
    set -e
    if [ "$rc" = 0 ]; then compiled=1; break; fi
    if [ "$rc" != 124 ]; then
        echo "fixture compile failed rc=$rc" >&2
        exit 1
    fi
    sleep 15
done

if [ "$compiled" != 1 ]; then
    echo "fixture build skipped (lock busy)" >&2
    exit 2
fi

"$PROTOBIN" | tee "$PROTOLOG"
"$FIXBIN" | tee "$LOG"
