#!/usr/bin/env bash
# Build the controlled UML kernel pair for experiment 0018:
#   fp-eager : current full owner GPU/parallel series (no 0018)
#   fp-lazy  : the exact same series + 0018 lazy-XSTATE (default OFF at boot)
#
# Only own sources/outputs are produced under the work directory. All kernel
# compiles are serialised on the GLOBAL lock /root/harddind-rebuild/
# kernel-build.lock shared with every other agent, and capped at -j4.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:-$repo/.work}
mode=${2:-both}
mkdir -p "$work"

case "$mode" in
    eager|lazy|both) ;;
    *) echo "usage: $0 [work] [eager|lazy|both]" >&2; exit 2 ;;
esac

# Global builder lock for the whole compile (fetch/extract need no lock).
exec 8>/root/harddind-rebuild/kernel-build.lock
flock -x 8

archive=/root/harddind-rebuild/cache/linux-7.2.10.tar.xz
[ -f "$archive" ] || archive=$work/cache/linux-7.2.10.tar.xz

prepare_tree() { # name flag
    local name=$1 flag=$2
    local tree=$work/$name/linux-7.2.10
    if [ ! -d "$tree" ]; then
        rm -rf "$work/$name"
        mkdir -p "$tree"
        tar -C "$tree" --strip-components=1 -xf "$archive"
        python3 "$repo/scripts/fp-apply-kernel-patches.py" "$tree" "$archive" "$flag" >/dev/null
    fi
    printf '%s\n' "$tree"
}

configure() { # tree out
    local tree=$1 out=$2 stage
    stage=$(mktemp -d "$out/.config-stage-XXXXXX")
    make -C "$tree" O="$stage" ARCH=um defconfig >/dev/null
    KCONFIG_CONFIG="$stage/.config" "$tree/scripts/kconfig/merge_config.sh" \
        -m -O "$stage" "$stage/.config" "$repo/kernel/runtime.config" >/dev/null
    make -C "$tree" O="$stage" ARCH=um olddefconfig >/dev/null
    python3 - "$repo/kernel/runtime.config" "$stage/.config" <<'PY'
import pathlib, sys
requested = pathlib.Path(sys.argv[1]).read_text().splitlines()
actual = set(pathlib.Path(sys.argv[2]).read_text().splitlines())
missing = [s for s in requested if s.startswith('CONFIG_') and s not in actual]
if missing:
    raise SystemExit('Required kernel options unavailable:\n' + '\n'.join(missing))
PY
    if ! cmp -s "$stage/.config" "$out/.config"; then
        cp "$stage/.config" "$out/.config"
    fi
    rm -rf "$stage"
}

build_one() { # flag name
    local flag=$1 name=$2
    local tree out
    tree=$(prepare_tree "$name" "$flag")
    out=$work/${name}-build
    mkdir -p "$out"
    out=$(realpath "$out")
    configure "$tree" "$out"
    make -C "$tree" O="$out" ARCH=um -j"${JOBS:-4}" linux
    python3 "$repo/scripts/check-stub-relocations.py" \
        "$out/arch/um/kernel/skas/stub.o"
    printf 'HARDDIND_FP_BUILD %s -> %s/linux\n' "$name" "$out"
}

if [ "$mode" = eager ] || [ "$mode" = both ]; then
    build_one --eager fp-eager
fi
if [ "$mode" = lazy ] || [ "$mode" = both ]; then
    build_one --lazy fp-lazy
fi
