#!/usr/bin/env bash
# Bounded probe compile under the global build lock (owner process-cleanup rule).
set -euo pipefail
cd /root/harddind-rebuild/arkclaude/round4/shared-posix-locks
exec 9>/root/harddind-rebuild/kernel-build.lock
flock -x 9
timeout --kill-after=10 120 gcc -O2 -Wall -Wextra -Werror -static src/share_probe.c -o work/probe/share-probe
