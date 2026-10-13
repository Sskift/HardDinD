#!/bin/bash
# Feature matrix for a udock install: runs real docker workflows through the
# `docker` on PATH and prints PASS/FAIL per feature.
#   test/features.sh
# Needs the alpine, nginx:alpine and hello-world images (pulled on demand).
set -u

work=$(mktemp -d /root/udock-features.XXXX)
trap 'docker rm -f $(docker ps -aq --filter label=udock-test) >/dev/null 2>&1; docker network rm udt-net >/dev/null 2>&1; docker volume rm udt-vol >/dev/null 2>&1; rm -rf "$work"' EXIT
pass=0 fail=0 failed=()

# check <name> <command...>: PASS if the command exits 0
check() {
  local name=$1
  shift
  if out=$("$@" 2>&1); then
    printf 'PASS  %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL  %s\n      %s\n' "$name" "$(echo "$out" | tail -n 2 | tr '\n' ' ' | cut -c1-160)"
    fail=$((fail + 1))
    failed+=("$name")
  fi
}

L=(--label udock-test)
R() { docker run --rm "${L[@]}" "$@"; }

# --- basics ---
check "run hello-world"            R hello-world
check "exit code propagates"       bash -c 'docker run --rm --label udock-test alpine sh -c "exit 3"; [ $? -eq 3 ]'
check "tty (-t)"                   bash -c 'docker run --rm -t --label udock-test alpine tty | grep -q /dev/pts'
check "stdin (-i)"                 bash -c 'echo piped | docker run --rm -i --label udock-test alpine cat | grep -qx piped'
check "detach/exec/logs/stop/start" bash -c '
  id=$(docker run -d --label udock-test alpine sh -c "echo started; sleep 300") &&
  docker exec "$id" true && sleep 1 && docker logs "$id" | grep -q started &&
  docker stop -t 1 "$id" >/dev/null && docker start "$id" >/dev/null &&
  [ "$(docker inspect -f {{.State.Running}} "$id")" = true ]'
check "docker top / stats"         bash -c '
  id=$(docker run -d --label udock-test alpine sleep 300) &&
  docker top "$id" | grep -q sleep && docker stats --no-stream "$id" >/dev/null'
check "--init"                     R --init alpine true
check "--user / --read-only"       bash -c 'docker run --rm --label udock-test --user 1000:1000 --read-only alpine sh -c "id -u | grep -qx 1000 && ! touch /x"'
check "--tmpfs"                    R --tmpfs /scratch alpine sh -c 'echo x > /scratch/f'
check "--ulimit"                   bash -c 'docker run --rm --label udock-test --ulimit nofile=512:512 alpine sh -c "ulimit -n" | grep -qx 512'
check "restart policy"             bash -c '
  id=$(docker run -d --label udock-test --restart on-failure:3 alpine sh -c "sleep 1; exit 1") &&
  sleep 8 && [ "$(docker inspect -f {{.RestartCount}} "$id")" -ge 1 ]'
check "healthcheck"                bash -c '
  id=$(docker run -d --label udock-test --health-cmd true --health-interval 1s alpine sleep 300) &&
  for i in $(seq 30); do [ "$(docker inspect -f {{.State.Health.Status}} "$id")" = healthy ] && exit 0; sleep 1; done; exit 1'

# --- isolation / limits ---
check "seccomp default profile"    bash -c 'docker run --rm --label udock-test alpine grep -q "Seccomp:[[:space:]]*2" /proc/self/status'
check "--memory (OOM kill)"        bash -c 'docker run --rm --label udock-test --memory 32m --memory-swap 32m alpine sh -c "head -c 200m /dev/zero | tail" ; [ $? -eq 137 ]'
# ash exits when fork fails, so fork in a subshell; and count via the cgroup,
# since pgrep would need a fork too once the limit is hit
check "--pids-limit"               bash -c 'docker run --rm --label udock-test --pids-limit 20 alpine sh -c "(for i in \$(seq 50); do sleep 5 & done) 2>/dev/null; read n < /sys/fs/cgroup/pids.current; [ \$n -le 20 ] && [ \$n -ge 15 ]"'
check "--cpus"                     R --cpus 0.5 alpine true
check "--cpuset-cpus"              R --cpuset-cpus 0 alpine true
check "--privileged (mount)"       R --privileged alpine sh -c 'mount -t tmpfs none /mnt && touch /mnt/x'
check "--cap-add NET_ADMIN"        R --cap-add NET_ADMIN alpine ip link add dummy0 type dummy
check "user namespace (unshare)"   R --privileged alpine unshare -U true

# --- storage ---
check "bind mount host dir (rw)"   bash -c "echo host > $work/in && docker run --rm --label udock-test -v $work:/w alpine sh -c 'cat /w/in && echo ctr > /w/out' && grep -qx ctr $work/out"
check "named volume"               bash -c 'docker volume create udt-vol >/dev/null &&
  docker run --rm --label udock-test -v udt-vol:/v alpine sh -c "echo kept > /v/f" &&
  docker run --rm --label udock-test -v udt-vol:/v alpine cat /v/f | grep -qx kept'
check "docker cp"                  bash -c "id=\$(docker run -d --label udock-test alpine sleep 300) && echo cp > $work/cp && docker cp $work/cp \$id:/cp && docker cp \$id:/cp $work/cp2 && cmp $work/cp $work/cp2"
check "commit / save / load"       bash -c 'id=$(docker run -d --label udock-test alpine sh -c "echo c > /c; sleep 300") &&
  docker commit "$id" udt-img:1 >/dev/null && docker save udt-img:1 -o '"$work"'/i.tar &&
  docker rmi udt-img:1 >/dev/null && docker load -i '"$work"'/i.tar >/dev/null &&
  docker run --rm --label udock-test udt-img:1 cat /c | grep -qx c && docker rmi udt-img:1 >/dev/null'
check "export / import"            bash -c 'id=$(docker create --label udock-test alpine) && docker export "$id" | docker import - udt-imp:1 >/dev/null && docker run --rm --label udock-test udt-imp:1 /bin/true && docker rmi udt-imp:1 >/dev/null'

# --- build ---
mkdir -p "$work/ctx"
cat > "$work/ctx/Dockerfile" <<'EOF'
FROM alpine AS stage
RUN echo from-stage > /artifact
FROM alpine
COPY --from=stage /artifact /artifact
COPY hello.txt /hello.txt
RUN --mount=type=cache,target=/cache echo cached > /cache/x
CMD cat /artifact /hello.txt
EOF
echo hello-ctx > "$work/ctx/hello.txt"
check "build (BuildKit, multi-stage, cache mount)" bash -c "docker build -q -t udt-build:1 $work/ctx >/dev/null && docker run --rm --label udock-test udt-build:1 | grep -q hello-ctx && docker rmi udt-build:1 >/dev/null"
check "buildx available"           docker buildx version
check "binfmt_misc (cross-arch builds)" R --privileged alpine sh -c 'mount -t binfmt_misc none /proc/sys/fs/binfmt_misc && [ -e /proc/sys/fs/binfmt_misc/register ]'
check "32-bit image (i386)"        R --platform linux/386 i386/alpine true

# --- networking ---
check "outbound DNS + TCP"         R alpine sh -c 'nslookup example.com >/dev/null && nc -z -w 10 example.com 80'
check "user network DNS by name"   bash -c 'docker network create udt-net >/dev/null &&
  docker run -d --label udock-test --network udt-net --name udt-web nginx:alpine >/dev/null && sleep 2 &&
  docker run --rm --label udock-test --network udt-net alpine wget -qO- http://udt-web/ | grep -qi nginx'
check "--network none"             bash -c '! docker run --rm --label udock-test --network none alpine nc -z -w 3 10.0.2.2 23760'
check "--network host (guest)"     R --network host alpine ip addr show vec0
check "-p publish (guest-side)"    bash -c 'docker run -d --label udock-test -p 18080:80 nginx:alpine >/dev/null && sleep 2 &&
  docker run --rm --label udock-test --network host alpine wget -qO- http://127.0.0.1:18080/ | grep -qi nginx'
check "IPv6 network"               bash -c 'docker network create --ipv6 udt-v6 >/dev/null && docker run --rm --label udock-test --network udt-v6 alpine sh -c "ip -6 addr | grep -q global"; rc=$?; docker network rm udt-v6 >/dev/null; exit $rc'

# --- compose / swarm ---
mkdir -p "$work/compose"
cat > "$work/compose/compose.yaml" <<'EOF'
services:
  db:
    image: alpine
    command: sh -c "touch /ready; sleep 300"
    healthcheck: { test: ["CMD", "test", "-e", "/ready"], interval: 1s, retries: 30 }
    labels: [udock-test]
  app:
    image: alpine
    depends_on: { db: { condition: service_healthy } }
    command: sh -c "nc -z -w 5 db 1 ; ping -c1 -W5 db"
    labels: [udock-test]
EOF
check "compose (healthcheck depends_on, service DNS)" bash -c "cd $work/compose && docker compose up --abort-on-container-exit --exit-code-from app >/dev/null 2>&1; rc=\$?; docker compose down -t 1 >/dev/null 2>&1; exit \$rc"
check "swarm + overlay network"    bash -c 'docker swarm init >/dev/null 2>&1 || true
  docker network create -d overlay --attachable udt-ov >/dev/null &&
  docker run --rm --label udock-test --network udt-ov alpine true; rc=$?
  docker network rm udt-ov >/dev/null 2>&1; docker swarm leave --force >/dev/null 2>&1; exit $rc'

echo
echo "$pass passed, $fail failed"
[ $fail -eq 0 ] || printf '  - %s\n' "${failed[@]}"
