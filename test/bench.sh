#!/bin/sh
# Micro-benchmarks run both inside a udock container and in a plain chroot of
# the same Alpine userland on the host, so the binaries are identical.
#   sh bench.sh <scratch-dir>
dir=${1:-/tmp/bench}
mkdir -p "$dir"

# busybox date has no %N and the chroot has no /proc, so use time -p
t() {
  time -p sh -c '"$@" >/dev/null 2>&1' _ "$@" 2>&1 | awk '/^real/ { print $2 "s" }'
}

echo "cpu: awk loop 2e7          $(t awk 'BEGIN { for (i = 0; i < 2e7; i++) s += i }')"
echo "syscalls: dd 4k x 200k     $(t dd if=/dev/zero of=/dev/null bs=4k count=200000)"
echo "fork+exec x1000            $(t sh -c 'i=0; while [ $i -lt 1000 ]; do /bin/true; i=$((i+1)); done')"
echo "disk: write 1GB + fsync    $(t dd if=/dev/zero of="$dir/big" bs=1M count=1024 conv=fsync)"
echo "disk: read 1GB             $(t dd if="$dir/big" of=/dev/null bs=1M)"
rm -f "$dir/big"
echo "fs: create 5k small files  $(t sh -c "cd '$dir' && i=0; while [ \$i -lt 5000 ]; do echo x > f\$i; i=\$((i+1)); done; sync")"
rm -rf "$dir"
