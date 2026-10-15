#!/usr/bin/env bash
# Build the HardDinD-pinned libslirp 4.6.1 as a static archive containing the
# bounded IPv6 fragmentation/reassembly backport
# (third_party/patches/0001-ipv6-fragmentation.patch).
#
# Upstream libslirp through 4.9.5 has no IPv6 fragmentation at all and pinned
# Ubuntu 4.6.1-1ubuntu0.2 is pristine upstream: next-header 44 fragments were
# freed in ip6_input(), and oversized guest-bound IPv6 packets were emitted
# without fragmentation.
#
# Locking is owned by the pipeline (./hd build); this script takes none.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=${1:?usage: build-libslirp.sh <work-directory> [asan]}
variant=${2:-}

python3 - "$repo" "$work" "$variant" <<'PY'
import json, os, pathlib, shutil, subprocess, sys, urllib.request, hashlib

repo, work, variant = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
lock = json.loads((repo / 'sources.lock.json').read_text())
nb = lock['network_backend']
tar_entry = nb['libslirp_source']
glib_entry = nb['libglib_dev']
cache = work / 'cache' / 'inputs'
cache.mkdir(parents=True, exist_ok=True)

def fetch(entry, dest):
    if dest.exists():
        return
    tmp = dest.with_suffix(dest.suffix + '.part')
    print(f'fetch {entry["url"]}', flush=True)
    urllib.request.urlretrieve(entry['url'], tmp)
    tmp.replace(dest)

def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

tarball = cache / pathlib.Path(tar_entry['url']).name
glibdeb = cache / pathlib.Path(glib_entry['url']).name
fetch(tar_entry, tarball)
fetch(glib_entry, glibdeb)
assert sha(tarball) == tar_entry['sha256'], 'libslirp tarball checksum mismatch'
assert sha(glibdeb) == glib_entry['sha256'], 'libglib dev deb checksum mismatch'

stage = work / 'libslirp-stage'
if stage.exists():
    shutil.rmtree(stage)
stage.mkdir(parents=True)
subprocess.run(['tar', 'xf', tarball, '-C', stage], check=True)
srcdir = stage / 'libslirp-v4.6.1'
# HardDinD patches, in application order:
#  1. IPv6 fragmentation backport (HardDinD)
#  2. Ubuntu 4.6.1-1ubuntu0.2 security patch CVE-2026-9539 (sosendoob urgent
#     data bounds / sb_cc underflow) - the production baseline links the
#     distro library, so a bare-upstream rebuild must carry it too.
for name in ('0001-ipv6-fragmentation.patch',
             '0002-cve-2026-9539-urgent-data-bounds.patch',
             '0003-net-vnet-mru.patch',
             '0004-net-window-scale.patch',
             '0005-net-large-tcp.patch'):
    patch_file = repo / 'third_party' / 'patches' / name
    r = subprocess.run(['patch', '-p1', '--batch', '--fuzz=0', '-d', srcdir,
                        '-i', patch_file], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f'libslirp patch failed: {name}')

glibroot = work / 'glib-headers'
if glibroot.exists():
    shutil.rmtree(glibroot)
subprocess.run(['dpkg-deb', '-x', glibdeb, glibroot], check=True)
glib_inc = glibroot / 'usr/include/glib-2.0'
glib_cfg = glibroot / 'usr/lib/x86_64-linux-gnu/glib-2.0/include'
assert glib_inc.is_dir() and glib_cfg.is_dir(), 'glib dev layout changed'

# Pinned wire/protocol version stays exactly "4.6.1".
(srcdir / 'src' / 'libslirp-version.h').write_text("""#ifndef LIBSLIRP_VERSION_H_
#define LIBSLIRP_VERSION_H_
#ifdef __cplusplus
extern "C" {
#endif
#define SLIRP_MAJOR_VERSION 4
#define SLIRP_MINOR_VERSION 6
#define SLIRP_MICRO_VERSION 1
#define SLIRP_VERSION_STRING "4.6.1"
#define SLIRP_CHECK_VERSION(major,minor,micro) \\
    (SLIRP_MAJOR_VERSION > (major) || \\
     (SLIRP_MAJOR_VERSION == (major) && SLIRP_MINOR_VERSION > (minor)) || \\
     (SLIRP_MAJOR_VERSION == (major) && SLIRP_MINOR_VERSION == (minor) && \\
      SLIRP_MICRO_VERSION >= (micro)))
#ifdef __cplusplus
}
#endif
#endif
""")

objdir = srcdir / 'obj'
objdir.mkdir(exist_ok=True)
san = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer'] if variant == 'asan' else []
opt = ['-O1', '-g'] if variant == 'asan' else ['-O2', '-g']
objects = []
for cfile in sorted((srcdir / 'src').glob('*.c')):
    o = objdir / (cfile.stem + '.o')
    subprocess.run(['gcc', '-D_GNU_SOURCE', '-DG_LOG_DOMAIN="Slirp"',
                    f'-I{glib_inc}', f'-I{glib_cfg}', f'-I{srcdir / "src"}',
                    *opt, *san, '-Wall', '-Wextra', '-fPIC', '-c', str(cfile), '-o', str(o)],
                   check=True)
    objects.append(str(o))

outdir = work / 'libslirp-harddind'
outdir.mkdir(parents=True, exist_ok=True)
archive = outdir / ('libslirp-asan.a' if variant == 'asan' else 'libslirp-harddind.a')
subprocess.run(['ar', 'rcs', archive, *objects], check=True)

# Build manifest: what was linked and from where, so the static archive is
# traceable to the pinned upstream tarball plus the distro security series.
import platform as _platform
manifest = {
    'library': 'libslirp',
    'reported_version': '4.6.1',
    'package_baseline': 'libslirp0:amd64 4.6.1-1ubuntu0.2',
    'variant': variant or 'release',
    'sanitizers': ['address', 'undefined'] if variant == 'asan' else [],
    'upstream_tarball': tar_entry,
    'applied_patches': [
        {'file': '0001-ipv6-fragmentation.patch',
         'purpose': 'HardDinD bounded IPv6 fragmentation/reassembly'},
        {'file': '0002-cve-2026-9539-urgent-data-bounds.patch',
         'purpose': 'sosendoob urgent data bounds / sb_cc underflow (Ubuntu)'},
        {'file': '0003-net-vnet-mru.patch',
         'purpose': 'Use configured receive limit for IPv6 GSO input'},
        {'file': '0004-net-window-scale.patch',
         'purpose': 'Negotiate larger TCP windows only for the virtual offload link'},
        {'file': '0005-net-large-tcp.patch',
         'purpose': 'Build TCP GSO superframes with guest MSS, preserve non-TCP MTU'},
    ],
    'objects': len(objects),
    'host': {'arch': _platform.machine(), 'compiler': 'gcc'},
    'link_note': 'release network-backend links this archive and the pinned glib dev archive statically; reported slirp version stays 4.6.1',
}
(outdir / 'BUILD_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'built {archive} ({len(objects)} objects, variant={variant or "release"})')
PY
