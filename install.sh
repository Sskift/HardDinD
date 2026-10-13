#!/bin/bash
# HardDinD installer: build and install udock into $UDOCK_HOME (default /root/udock).
#
#   curl -fsSL https://raw.githubusercontent.com/Sskift/HardDinD/main/install.sh | bash
#   ./install.sh            # from a checkout
#
# Environment:
#   UDOCK_HOME    install dir (default /root/udock)
#   UDOCK_CACHE   dir with already-downloaded tarballs/binaries to reuse
#   NO_LINK=1     don't create /usr/local/bin/{udock,docker}
#   NO_START=1    don't boot the guest and run hello-world at the end
#   KEEP_BUILD=1  keep the kernel build tree (~1.5G) for later rebuilds
#   DISK_SIZE     guest disk size, sparse (default 200G)
set -euo pipefail

KERNEL_VERSION=6.6.158
DOCKER_VERSION=29.9.0
COMPOSE_VERSION=v5.6.0
BUILDX_VERSION=v0.38.0
REPO=https://github.com/Sskift/HardDinD

UDOCK_HOME=${UDOCK_HOME:-/root/udock}
UDOCK_CACHE=${UDOCK_CACHE:-$UDOCK_HOME/dl}
DISK_SIZE=${DISK_SIZE:-200G}

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

# fetch <url> <file>: resumable, retried download, reused from UDOCK_CACHE
fetch() {
  local url=$1 out=$2 name i
  name=$(basename "$out")
  if [ -s "$UDOCK_CACHE/$name" ] && [ "$UDOCK_CACHE/$name" != "$out" ]; then
    cp "$UDOCK_CACHE/$name" "$out"
    return
  fi
  for i in 1 2 3 4 5 6; do
    curl -fL --retry 3 -C - -o "$out.part" "$url" && { mv "$out.part" "$out"; return; }
    echo "retrying $name ($i)" >&2
    sleep 2
  done
  die "download failed: $url"
}

# --- preflight ---
[ "$(id -u)" = 0 ] || die "run as root (inside the container)"
[ "$(uname -m)" = x86_64 ] || die "only x86_64 is supported"
python3 - <<'EOF' 2>/dev/null || die "ptrace is blocked here; User-Mode Linux needs it"
import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
pid = os.fork()
if pid == 0:
    os._exit(0 if libc.ptrace(0, 0, 0, 0) == 0 else 1)
_, st = os.waitpid(pid, 0)
raise SystemExit(os.WEXITSTATUS(st))
EOF

mkdir -p "$UDOCK_HOME"/{bin,tmp,dl} "$UDOCK_CACHE"

# --- source ---
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/udock" ] && [ -f "$here/net/udock-net.c" ]; then
  src=$here
  if [ "$src" != "$UDOCK_HOME/src" ]; then
    say "copying source from $src"
    rm -rf "$UDOCK_HOME/src" && mkdir -p "$UDOCK_HOME/src"
    cp -a "$src"/. "$UDOCK_HOME/src/"
  fi
else
  say "downloading source"
  rm -rf "$UDOCK_HOME/src" && mkdir -p "$UDOCK_HOME/src"
  curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 -C "$UDOCK_HOME/src"
fi
src=$UDOCK_HOME/src

# --- packages ---
if command -v apt-get >/dev/null; then
  # only what's missing: 'apt-get install' would also upgrade installed
  # packages, which fails on a stale index
  missing=()
  for pkg in build-essential flex bison bc libslirp-dev uml-utilities e2fsprogs curl xz-utils openssl; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    say "installing ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -q "${missing[@]}" >/dev/null ||
      { apt-get update -q >/dev/null && apt-get install -y -q "${missing[@]}" >/dev/null; } ||
      die "apt-get install ${missing[*]} failed"
  fi
else
  for t in gcc make flex bison bc uml_mconsole mkfs.ext4 debugfs curl xz openssl; do
    command -v $t >/dev/null || die "missing $t (and libslirp headers); install them first"
  done
fi

# --- kernel ---
if [ ! -x "$UDOCK_HOME/vmlinux-udock" ]; then
  say "building UML kernel $KERNEL_VERSION (a few minutes)"
  tarball=$UDOCK_HOME/dl/linux-$KERNEL_VERSION.tar.xz
  fetch "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KERNEL_VERSION.tar.xz" "$tarball"
  rm -rf "$UDOCK_HOME/linux-$KERNEL_VERSION"
  tar xf "$tarball" -C "$UDOCK_HOME"
  "$src/kernel/build.sh" "$UDOCK_HOME/linux-$KERNEL_VERSION" "$UDOCK_HOME/vmlinux-udock"
  [ "${KEEP_BUILD:-0}" = 1 ] || rm -rf "$UDOCK_HOME/linux-$KERNEL_VERSION"
fi

# --- udock-net ---
say "building udock-net"
"$src/net/build.sh" "$UDOCK_HOME/bin/udock-net"

# --- docker client + plugins ---
if [ ! -x "$UDOCK_HOME/bin/docker-cli" ]; then
  say "downloading docker $DOCKER_VERSION client"
  fetch "https://download.docker.com/linux/static/stable/x86_64/docker-$DOCKER_VERSION.tgz" "$UDOCK_HOME/dl/docker-$DOCKER_VERSION.tgz"
  tar xzf "$UDOCK_HOME/dl/docker-$DOCKER_VERSION.tgz" -C "$UDOCK_HOME/dl" docker/docker
  install -m 755 "$UDOCK_HOME/dl/docker/docker" "$UDOCK_HOME/bin/docker-cli"
  rm -rf "$UDOCK_HOME/dl/docker"
fi
plugins=${HOME:-/root}/.docker/cli-plugins
mkdir -p "$plugins"
if [ ! -x "$plugins/docker-compose" ]; then
  say "downloading compose $COMPOSE_VERSION"
  fetch "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/docker-compose-linux-x86_64" "$UDOCK_HOME/dl/docker-compose"
  install -m 755 "$UDOCK_HOME/dl/docker-compose" "$plugins/docker-compose"
fi
if [ ! -x "$plugins/docker-buildx" ]; then
  say "downloading buildx $BUILDX_VERSION"
  fetch "https://github.com/docker/buildx/releases/download/$BUILDX_VERSION/buildx-$BUILDX_VERSION.linux-amd64" "$UDOCK_HOME/dl/docker-buildx"
  install -m 755 "$UDOCK_HOME/dl/docker-buildx" "$plugins/docker-buildx"
fi

# --- guest disk ---
if [ ! -e "$UDOCK_HOME/disk.img" ]; then
  say "building guest disk (Alpine + dockerd)"
  "$src/build-rootfs.sh" "$UDOCK_HOME" "$DISK_SIZE"
fi

# --- commands ---
if [ "${NO_LINK:-0}" != 1 ]; then
  ln -sf "$src/udock" /usr/local/bin/udock
  if [ -e /usr/local/bin/docker ] && [ "$(readlink -f /usr/local/bin/docker)" != "$src/docker" ]; then
    echo "install: /usr/local/bin/docker exists and isn't udock's; leaving it alone" >&2
  else
    ln -sf "$src/docker" /usr/local/bin/docker
  fi
fi

if [ "${NO_START:-0}" != 1 ]; then
  say "booting the guest"
  UDOCK_HOME=$UDOCK_HOME "$src/udock" start
  eval "$(UDOCK_HOME=$UDOCK_HOME "$src/udock" env)"
  "$UDOCK_HOME/bin/docker-cli" run --rm hello-world | grep -m1 "Hello from Docker"
fi
say "done: run 'docker ...' (or 'udock status')"
