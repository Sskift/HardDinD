#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
root=$work/docker-root
disk=$work/docker.ext4
[ "$(id -u)" = 0 ] || { echo 'container root is required to prepare ownership and chroot' >&2; exit 1; }
[ ! -e "$disk" ] || { echo 'refusing to replace an existing disk' >&2; exit 1; }
archive=$(python3 "$repo/scripts/fetch-artifact.py" alpine "$work/cache")
if [ ! -e "$root" ]; then
    mkdir -p "$root"
    tar -xf "$archive" -C "$root"
fi
cp /etc/resolv.conf "$root/etc/resolv.conf"
mapfile -t packages < <(python3 - "$repo/sources.lock.json" <<'PY'
import json,sys
print('\n'.join(json.load(open(sys.argv[1]))['alpine_packages']))
PY
)
timeout 180 chroot "$root" apk add --no-cache "${packages[@]}"
mkdir -p "$work/results"
chroot "$root" apk info -v > "$work/results/docker-packages.txt"
python3 - "$repo/sources.lock.json" "$work/results/docker-packages.txt" <<'PY'
import json,pathlib,sys
expected = set(json.load(open(sys.argv[1]))['alpine_package_manifest'])
actual = set(pathlib.Path(sys.argv[2]).read_text().splitlines())
if expected != actual:
    raise SystemExit('Package versions drifted from sources.lock.json: ' +
                     repr({'missing': sorted(expected-actual), 'unexpected': sorted(actual-expected)}))
PY
gcc -O2 -Wall -Wextra -Werror -static -pthread "$repo/src/corebench.c" -o "$root/corebench"
gcc -O2 -Wall -Wextra -Werror -static -pthread "$repo/src/thread_semantics.c" -o "$root/thread-semantics"
gcc -O2 -Wall -Wextra -Werror -static -pthread "$repo/src/dispatchbench.c" -o "$root/dispatchbench"
gcc -O2 -Wall -Wextra -Werror "$repo/src/cuda_probe.c" -ldl -o "$root/cuda-probe"
python3 "$repo/scripts/make-gpu-fixture.py" "$work"
install -m 755 "$repo/tests/docker-init" "$root/sbin/harddind-docker-init"
install -m 755 "$repo/tests/docker-suite.sh" "$root/docker-suite.sh"
cp "$archive" "$root/alpine.tar.gz"
truncate -s 4G "$disk"
mkfs.ext4 -q -F -d "$root" "$disk"
sha256sum "$root/corebench" "$root/cuda-probe"
