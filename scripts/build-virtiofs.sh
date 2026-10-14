#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
toolchain=$(realpath "${2:?path to the pinned Rust toolchain required}")
exec 9>"$work/kernel-build.lock"
flock -x 9
export PATH="$toolchain/bin:$PATH"
export RUSTC="$toolchain/bin/rustc"
python3 - "$repo/sources.lock.json" "$RUSTC" <<'PY'
import json,subprocess,sys
expected=json.load(open(sys.argv[1]))['rust_build']
actual=dict(line.split(': ',1) for line in subprocess.check_output([sys.argv[2],'-vV'],text=True).splitlines() if ': ' in line)
if any(actual.get(k)!=v for k,v in {'release':expected['version'],'commit-hash':expected['commit'],'host':expected['host']}.items()):
    raise SystemExit('Rust toolchain does not match sources.lock.json')
PY
archive=$(python3 "$repo/scripts/fetch-artifact.py" virtiofsd "$work/cache")
src="$work/virtiofsd-1.13.2"
if [ ! -d "$src" ]; then
    stage=$(mktemp -d "$work/virtiofsd-source-XXXXXX")
    tar -xf "$archive" -C "$stage" --strip-components=1
    mv "$stage" "$src"
fi
native="$work/virtiofs-native"
mkdir -p "$native"
for key in virtiofs_libcapng virtiofs_libcapng_dev virtiofs_libseccomp virtiofs_libseccomp_dev; do
    deb=$(python3 "$repo/scripts/fetch-artifact.py" "$key" "$work/cache")
    dpkg-deb -x "$deb" "$native"
done
export CARGO_HOME="$work/cache/cargo-virtiofs" CARGO_TARGET_DIR="$work/virtiofsd-build"
export CARGO_BUILD_JOBS=8 CARGO_NET_RETRY=2
export PKG_CONFIG_SYSROOT_DIR="$native" PKG_CONFIG_LIBDIR="$native/usr/lib/x86_64-linux-gnu/pkgconfig"
export RUSTFLAGS="-Lnative=$native/usr/lib/x86_64-linux-gnu -Lnative=$native/lib/x86_64-linux-gnu -C link-arg=-Wl,-rpath,$native/usr/lib/x86_64-linux-gnu -C link-arg=-Wl,-rpath,$native/lib/x86_64-linux-gnu"
# Cargo.lock includes exact registry versions and source checksums. Never update it.
(cd "$src" && "$toolchain/bin/cargo" build --release --locked)
install -m 755 "$CARGO_TARGET_DIR/release/virtiofsd" "$work/virtiofsd.new"
"$work/virtiofsd.new" --version
mv "$work/virtiofsd.new" "$work/virtiofsd"
sha256sum "$archive" "$src/Cargo.lock" "$work/virtiofsd" "$toolchain/bin/rustc" "$toolchain/bin/cargo"
ldd "$work/virtiofsd"
