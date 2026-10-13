#!/bin/bash
# Build the udock guest disk: Alpine + docker, plus TLS certs for the API.
#   ./build-rootfs.sh <udock-home> [disk-size]
# Writes <udock-home>/disk.img and <udock-home>/certs/{ca,cert,key}.pem.
set -euo pipefail

ALPINE_BRANCH=v3.24
ALPINE_VERSION=3.24.2

home=$(realpath "$1")
size=${2:-200G}
here=$(realpath "$(dirname "$0")")
[ ! -e "$home/disk.img" ] || { echo "$home/disk.img already exists" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root=$work/root
mkdir -p "$root" "$home/certs"

curl -fsSL "https://dl-cdn.alpinelinux.org/alpine/$ALPINE_BRANCH/releases/x86_64/alpine-minirootfs-$ALPINE_VERSION-x86_64.tar.gz" \
  | tar xz -C "$root"
cp /etc/resolv.conf "$root/etc/resolv.conf"
chroot "$root" /sbin/apk add --no-cache -q docker tini iptables ip6tables ca-certificates

install -m 755 "$here/guest/udock-init" "$root/sbin/udock-init"
install -m 755 "$here/guest/udock-run" "$root/sbin/udock-run"

# Mutual TLS: slirp can only forward the API port on 0.0.0.0, so every
# client must present a cert signed by this CA.
certs=$home/certs
if [ ! -e "$certs/ca.pem" ]; then
  (
    cd "$certs"
    openssl req -x509 -new -nodes -newkey rsa:4096 -days 3650 -subj /CN=udock-ca \
      -keyout ca-key.pem -out ca.pem 2>/dev/null
    openssl req -new -nodes -newkey rsa:4096 -subj /CN=udock-server \
      -keyout server-key.pem -out server.csr 2>/dev/null
    openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 3650 \
      -extfile <(printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth') \
      -out server-cert.pem 2>/dev/null
    openssl req -new -nodes -newkey rsa:4096 -subj /CN=udock-client \
      -keyout key.pem -out client.csr 2>/dev/null
    openssl x509 -req -in client.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 3650 \
      -extfile <(printf 'extendedKeyUsage=clientAuth') -out cert.pem 2>/dev/null
    rm -f ./*.csr ca.srl
    chmod 600 ./*-key.pem key.pem
  )
fi
mkdir -p "$root/etc/udock"
cp "$certs/ca.pem" "$certs/server-cert.pem" "$certs/server-key.pem" "$root/etc/udock/"

echo "nameserver 10.0.2.3" > "$root/etc/resolv.conf"
truncate -s "$size" "$home/disk.img"
mkfs.ext4 -q -L udock -d "$root" "$home/disk.img"
echo "built $home/disk.img ($size sparse)"
