#!/bin/bash
# Build udock-net: ./build.sh <out-binary>   (needs libslirp-dev)
set -euo pipefail
cd "$(dirname "$0")"
gcc -O2 -Wall -Wextra -o "$1" udock-net.c -lslirp
