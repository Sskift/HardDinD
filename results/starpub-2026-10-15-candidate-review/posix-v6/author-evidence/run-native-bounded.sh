#!/usr/bin/env bash
# Bounded native matrix under the global build/test lock (owner rule).
set -euo pipefail
cd /root/harddind-rebuild/arkclaude/round4/shared-posix-locks
exec 9>/root/harddind-rebuild/kernel-build.lock
flock -x 9
rm -rf work/posix-native-share work/results
timeout --kill-after=20 180 python3 scripts/run-posix-native.py work --name posix-native-v15
