set -eu
cd /root/harddind-rebuild/publish-diag-v1
gcc -O2 -Wall -Wextra -Werror network_backend.c -lslirp -o run/network-backend
gcc -O2 -Wall -Wextra -Werror -static client6.c -o client6
readelf -d /root/harddind-rebuild/protect-docker-root/bin/busybox
