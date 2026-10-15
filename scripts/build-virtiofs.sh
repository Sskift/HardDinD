#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(realpath "${1:?work directory required}")
toolchain=$(realpath "${2:?path to the pinned Rust toolchain required}")
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
patch_sha=$(cat "$repo"/virtiofs/patches/[0-9]*.patch | sha256sum | cut -d' ' -f1)
# Always unpack verified input so cached local edits cannot change the build.
src=$(mktemp -d "$work/virtiofsd-source-XXXXXX")
trap 'rm -rf "$src"' EXIT
tar -xf "$archive" -C "$src" --strip-components=1
for patch_file in "$repo"/virtiofs/patches/[0-9]*.patch; do
    patch --batch --fuzz=0 -d "$src" -p1 < "$patch_file"
done
native="$work/virtiofs-native"
mkdir -p "$native"
for key in virtiofs_libcapng virtiofs_libcapng_dev virtiofs_libseccomp virtiofs_libseccomp_dev; do
    deb=$(python3 "$repo/scripts/fetch-artifact.py" "$key" "$work/cache")
    dpkg-deb -x "$deb" "$native"
done
export CARGO_HOME="$work/cache/cargo-virtiofs" CARGO_TARGET_DIR="$work/virtiofsd-build"
# Starpub is shared with other tenants; cap at four jobs.
export CARGO_BUILD_JOBS=4 CARGO_NET_RETRY=2
export PKG_CONFIG_SYSROOT_DIR="$native" PKG_CONFIG_LIBDIR="$native/usr/lib/x86_64-linux-gnu/pkgconfig"
export RUSTFLAGS="-Lnative=$native/usr/lib/x86_64-linux-gnu -Lnative=$native/lib/x86_64-linux-gnu -C link-arg=-Wl,-rpath,$native/usr/lib/x86_64-linux-gnu -C link-arg=-Wl,-rpath,$native/lib/x86_64-linux-gnu"
# Cargo.lock includes exact registry versions and source checksums. Never update it.
# Bounded so a hung lock test cannot stall the pipeline.
(cd "$src" && timeout --kill-after=30s 180s "$toolchain/bin/cargo" test --release --locked harddind_ -- --test-threads=1)
(cd "$src" && timeout --kill-after=30s 600s "$toolchain/bin/cargo" build --release --locked)
install -m 755 "$CARGO_TARGET_DIR/release/virtiofsd" "$work/virtiofsd.new"
"$work/virtiofsd.new" --version
mv "$work/virtiofsd.new" "$work/virtiofsd"
sha256sum "$archive" "$src/Cargo.lock" "$work/virtiofsd" "$toolchain/bin/rustc" "$toolchain/bin/cargo"
printf 'patch_series_sha256=%s\n' "$patch_sha"
ldd "$work/virtiofsd"
