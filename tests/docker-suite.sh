#!/bin/sh
# A growing acceptance suite. Passing this subset is not full Docker acceptance.
set -u
fail=0
check() {
    name=$1; shift
    if "$@" > /run/check.out 2>&1; then
        printf 'HARDDIND_RESULT {"name":"%s","status":"pass"}\n' "$name"
    else
        printf 'HARDDIND_RESULT {"name":"%s","status":"fail"}\n' "$name"
        tail -8 /run/check.out
        fail=$((fail+1))
    fi
}
check image_import sh -c 'docker import /alpine.tar.gz harddind-base:test >/dev/null'
check run docker run --rm harddind-base:test /bin/true
check exit_code sh -c 'docker run --rm harddind-base:test /bin/sh -c "exit 37"; [ $? -eq 37 ]'
check stdin sh -c 'printf "input-test\n" | docker run --rm -i harddind-base:test /bin/cat | grep -qx input-test'
check tty sh -c 'docker run --rm -t harddind-base:test /usr/bin/tty | grep -q /dev/pts'
check lifecycle sh -c '
 id=$(docker run -d harddind-base:test /bin/sh -c "echo started; sleep 300") &&
 docker exec "$id" /bin/true && docker logs "$id" | grep -qx started &&
 docker stop -t 1 "$id" >/dev/null && docker start "$id" >/dev/null &&
 docker kill "$id" >/dev/null && docker rm "$id" >/dev/null'
check mount_tmpfs docker run --rm --tmpfs /scratch harddind-base:test /bin/sh -c 'echo okay >/scratch/test; test -f /scratch/test'
check privileged_mount docker run --rm --privileged harddind-base:test /bin/sh -c 'mount -t tmpfs none /mnt && touch /mnt/test'
check net_admin docker run --rm --cap-add NET_ADMIN harddind-base:test /sbin/ip link add test0 type dummy
check guest_user_namespace docker run --rm --privileged harddind-base:test /usr/bin/unshare -U /bin/true
check read_only sh -c 'docker run --rm --read-only --user 1000:1000 harddind-base:test /bin/sh -c "! touch /forbidden"'
check bind_mount sh -c '
 mkdir -p /run/bind; echo before >/run/bind/in
 docker run --rm -v /run/bind:/data harddind-base:test /bin/sh -c "cat /data/in; echo after >/data/out" &&
 grep -qx after /run/bind/out'
check volume sh -c '
 docker volume create lab-volume >/dev/null &&
 docker run --rm -v lab-volume:/data harddind-base:test /bin/sh -c "echo persisted >/data/test" &&
 docker run --rm -v lab-volume:/data harddind-base:test /bin/cat /data/test | grep -qx persisted'
check memory_oom sh -c '
 docker run --rm --memory 32m --memory-swap 32m harddind-base:test /bin/sh -c "head -c 256m /dev/zero | tail" >/dev/null
 [ $? -eq 137 ]'
check pids_limit sh -c '
 docker run --rm --pids-limit 10 harddind-base:test /bin/sh -c '\''(for i in $(seq 30); do sleep 5 & done) 2>/dev/null; read n < /sys/fs/cgroup/pids.current; [ "$n" -le 10 ] && [ "$n" -ge 5 ]'\'''
check default_seccomp docker run --rm harddind-base:test /bin/sh -c 'grep -q "Seccomp:[[:space:]]*2" /proc/self/status'
check container_threads docker run --rm -v /corebench:/corebench:ro harddind-base:test /corebench threads 4
echo "HARDDIND_THREAD_SHA256=$(sha256sum /thread-semantics | cut -d' ' -f1)"
check container_thread_semantics docker run --rm -v /thread-semantics:/thread-semantics:ro harddind-base:test /thread-semantics
check container_compute docker run --rm -v /corebench:/corebench:ro harddind-base:test /corebench processes 4
check bridge_dns sh -c '
 docker network create lab-net >/dev/null &&
 docker run -d --network lab-net --name lab-peer harddind-base:test /bin/sleep 300 >/dev/null &&
 docker run --rm --network lab-net harddind-base:test /bin/ping -c 1 -W 3 lab-peer'
check published_port_guest sh -c '
 docker run -d -p 18080:80 --name lab-http harddind-base:test /bin/sh -c '\''while :; do printf "HTTP/1.0 200 OK\r\nContent-Length: 9\r\n\r\nharddind\n" | nc -l -p 80; done'\'' >/dev/null &&
 for i in $(seq 10); do wget -qO- -T 2 http://127.0.0.1:18080/ | grep -qx harddind && exit 0; sleep 1; done; docker logs lab-http; exit 1'
check docker_cp sh -c '
 id=$(docker create harddind-base:test /bin/true) &&
 echo copied >/run/copied && docker cp /run/copied "$id":/copied &&
 docker cp "$id":/copied /run/copied-back && cmp /run/copied /run/copied-back'
check save_load sh -c 'docker save harddind-base:test -o /run/image.tar && docker load -i /run/image.tar >/dev/null'
mkdir -p /run/build-context
cat > /run/build-context/Dockerfile <<'EOF'
FROM harddind-base:test AS compile
RUN --mount=type=cache,target=/cache echo built >/artifact
FROM harddind-base:test
COPY --from=compile /artifact /artifact
CMD ["/bin/cat", "/artifact"]
EOF
check buildkit_multistage sh -c 'docker build --pull=false -t harddind-built:test /run/build-context && docker run --rm harddind-built:test | grep -qx built'
check restart_policy sh -c '
 id=$(docker run -d --restart on-failure:2 harddind-base:test /bin/sh -c "sleep 1; exit 1") &&
 sleep 6 && n=$(docker inspect -f "{{.RestartCount}}" "$id") && [ "$n" -ge 1 ]'
check healthcheck sh -c '
 id=$(docker run -d --health-cmd "test -f /ready" --health-interval 1s harddind-base:test /bin/sh -c "touch /ready; sleep 60") &&
 for i in $(seq 20); do [ "$(docker inspect -f "{{.State.Health.Status}}" "$id")" = healthy ] && exit 0; sleep 1; done; exit 1'
check ipv6 sh -c '
 docker network create --ipv6 lab-v6 >/dev/null &&
 docker run --rm --network lab-v6 harddind-base:test /sbin/ip -6 addr | grep -q global'
check signals docker run --rm -v /corebench:/corebench:ro harddind-base:test /corebench signals
# Must remain failing until the candidate can execute CUDA inside a container.
check gpu_fixture_import sh -c 'docker import /gpu-rootfs.tar harddind-gpu:test >/dev/null'
check gpu_compute docker run --rm --gpus all harddind-gpu:test /cuda-probe
printf 'HARDDIND_RESULT {"name":"external_network","status":"not_tested"}\n'
printf 'HARDDIND_RESULT {"name":"full_docker_compatibility","status":"not_tested"}\n'
[ "$fail" -eq 0 ]
