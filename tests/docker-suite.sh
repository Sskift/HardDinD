#!/bin/sh
# A growing acceptance suite. Passing this subset is not full Docker acceptance.
set -u
fail=0
check() {
    name=$1; shift
    if [ -n "${HARDDIND_CHECK_FILTER:-}" ] && ! printf '%s\n' "$name" | grep -Eq "$HARDDIND_CHECK_FILTER"; then
        printf 'HARDDIND_RESULT {"name":"%s","status":"not_tested"}\n' "$name"
        return
    fi
    if timeout 90 "$@" > /run/check.out 2>&1; then
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
echo "HARDDIND_DISPATCH_SHA256=$(sha256sum /dispatchbench | cut -d' ' -f1)"
check container_dispatch_semantics docker run --rm -v /dispatchbench:/dispatchbench:ro harddind-base:test /bin/sh -c '
 for test in identity fdio mapping thread_syscalls isolation parallel_exec; do /dispatchbench "$test" || exit 1; done'
echo "HARDDIND_MAPPING_SHA256=$(sha256sum /mapping-semantics | cut -d' ' -f1)"
check container_mapping_semantics docker run --rm -v /mapping-semantics:/mapping-semantics:ro harddind-base:test /mapping-semantics
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

# Exercise command effects, rather than accepting help text or an HTTP 200.
check inspect_rename sh -c '
 id=$(docker create --name lab-original --label harddind=verified -e ANSWER=42 -w /tmp harddind-base:test /bin/true) &&
 docker rename "$id" lab-renamed &&
 [ "$(docker inspect -f "{{.Name}} {{index .Config.Labels \"harddind\"}} {{.Config.WorkingDir}}" "$id")" = "/lab-renamed verified /tmp" ] &&
 docker rm "$id" >/dev/null'
check restart sh -c '
 id=$(docker run -d harddind-base:test /bin/sleep 300) &&
 before=$(docker inspect -f "{{.State.StartedAt}}" "$id") &&
 docker restart -t 1 "$id" >/dev/null &&
 [ "$(docker inspect -f "{{.State.Running}}" "$id")" = true ] &&
 [ "$(docker inspect -f "{{.State.StartedAt}}" "$id")" != "$before" ] && docker rm -f "$id" >/dev/null'
check pause_unpause sh -c '
 mkdir -p /run/freezer
 id=$(docker run -d -v /run/freezer:/counter harddind-base:test /bin/sh -c '\''i=0; while :; do i=$((i+1)); echo "$i" >/counter/next; mv /counter/next /counter/value; sleep 0.05; done'\'') &&
 for i in $(seq 20); do [ -s /run/freezer/value ] && break; sleep 0.1; done
 docker pause "$id" >/dev/null &&
 [ "$(docker inspect -f "{{.State.Paused}}" "$id")" = true ] &&
 first=$(cat /run/freezer/value) && sleep 0.2 && [ "$(cat /run/freezer/value)" = "$first" ] &&
 docker unpause "$id" >/dev/null && sleep 0.2 && [ "$(cat /run/freezer/value)" -gt "$first" ] &&
 docker rm -f "$id" >/dev/null'
check update_limits sh -c '
 id=$(docker run -d --memory 64m --memory-swap 64m --pids-limit 64 harddind-base:test /bin/sleep 300) &&
 docker update --memory 96m --memory-swap 96m --pids-limit 48 --cpus 0.5 "$id" >/dev/null &&
 docker exec "$id" /bin/sh -c '\''[ "$(cat /sys/fs/cgroup/memory.max)" = 100663296 ] && [ "$(cat /sys/fs/cgroup/pids.max)" = 48 ] && [ "$(cat /sys/fs/cgroup/cpu.max)" = "50000 100000" ]'\'' &&
 docker rm -f "$id" >/dev/null'
check top_stats sh -c '
 id=$(docker run -d harddind-base:test /bin/sleep 300) &&
 docker top "$id" | grep -q sleep &&
 docker stats --no-stream --format "{{.ID}} {{.PIDs}}" "$id" | grep -Eq "[0-9a-f]+ [1-9][0-9]*" &&
 docker rm -f "$id" >/dev/null'
check wait_exit sh -c '
 id=$(docker run -d harddind-base:test /bin/sh -c "sleep 1; exit 23") &&
 [ "$(docker wait "$id")" = 23 ] &&
 [ "$(docker inspect -f "{{.State.ExitCode}}" "$id")" = 23 ] && docker rm "$id" >/dev/null'
check attach sh -c '
 id=$(docker create -i harddind-base:test /bin/cat) && docker start "$id" >/dev/null &&
 printf "attach-roundtrip\n" | timeout 10 docker attach "$id" | grep -qx attach-roundtrip &&
 docker rm -f "$id" >/dev/null'
check commit_diff_history sh -c '
 id=$(docker run -d harddind-base:test /bin/sleep 300) &&
 docker exec "$id" /bin/sh -c "echo committed >/new-file" &&
 docker diff "$id" | grep -q "A /new-file" &&
 docker commit "$id" harddind-committed:test >/dev/null &&
 docker history --format "{{.ID}}" harddind-committed:test | grep -q . &&
 docker run --rm harddind-committed:test /bin/cat /new-file | grep -qx committed &&
 docker rm -f "$id" >/dev/null'
check export_import sh -c '
 id=$(docker create harddind-committed:test /bin/true) &&
 docker export "$id" -o /run/export.tar &&
 docker import /run/export.tar harddind-exported:test >/dev/null &&
 docker run --rm harddind-exported:test /bin/cat /new-file | grep -qx committed &&
 docker rm "$id" >/dev/null'
check image_tag_remove sh -c '
 docker tag harddind-base:test harddind-tagged:test &&
 [ "$(docker image inspect -f "{{.Id}}" harddind-base:test)" = "$(docker image inspect -f "{{.Id}}" harddind-tagged:test)" ] &&
 docker rmi harddind-tagged:test >/dev/null && ! docker image inspect harddind-tagged:test'
check events sh -c '
 docker events --filter label=harddind-event=1 --format "{{.Action}}" >/run/events.out & listener=$!
 sleep 1
 id=$(docker create --label harddind-event=1 harddind-base:test /bin/true) &&
 docker start "$id" >/dev/null && docker wait "$id" >/dev/null && docker rm "$id" >/dev/null || exit 1
 sleep 1; kill "$listener"; wait "$listener" || true
 grep -qx create /run/events.out && grep -qx start /run/events.out && grep -qx die /run/events.out && grep -qx destroy /run/events.out'
check network_connect_disconnect sh -c '
 docker network create lab-extra >/dev/null &&
 id=$(docker run -d harddind-base:test /bin/sleep 300) &&
 docker network connect --alias attached lab-extra "$id" &&
 docker run --rm --network lab-extra harddind-base:test /bin/ping -c 1 -W 3 attached &&
 docker network disconnect lab-extra "$id" &&
 [ "$(docker inspect -f "{{if index .NetworkSettings.Networks \"lab-extra\"}}present{{else}}absent{{end}}" "$id")" = absent ] &&
 docker rm -f "$id" >/dev/null && docker network rm lab-extra >/dev/null'
check port_inspect sh -c 'docker port lab-http 80/tcp | grep -q ":18080$"'
check context sh -c '
 docker context create lab-context --docker host=unix:///run/docker.sock >/dev/null &&
 docker --context lab-context info --format "{{.ServerVersion}}" | grep -qx 29.8.2 &&
 docker context rm lab-context >/dev/null'
check system_df sh -c 'docker system df --format "{{.Type}} {{.TotalCount}}" | grep -q Images'
check prune_objects sh -c '
 id=$(docker create --label harddind-prune=1 harddind-base:test /bin/true) &&
 docker container prune -f --filter label=harddind-prune=1 >/dev/null && ! docker inspect "$id" &&
 docker volume create --label harddind-prune=1 lab-prune >/dev/null &&
 docker volume prune -a -f --filter label=harddind-prune=1 >/dev/null && ! docker volume inspect lab-prune'

# A real single-manager Swarm exercises the embedded control plane and VIP
# networking. Multiple-node behavior still needs separate acceptance.
check swarm_init sh -c '
 ip link add lab-swarm type dummy && ip addr add 10.42.0.1/24 dev lab-swarm && ip link set lab-swarm up &&
 docker swarm init --advertise-addr 10.42.0.1 --listen-addr 10.42.0.1:2377 >/dev/null &&
 [ "$(docker info -f "{{.Swarm.LocalNodeState}}")" = active ] && docker node ls --format "{{.Status}}" | grep -qx Ready &&
 docker network create --driver overlay --attachable lab-overlay >/dev/null'
check swarm_service sh -c '
 docker service create --detach --name lab-service --network lab-overlay --no-resolve-image --replicas 2 harddind-base:test /bin/sh -c '\''while :; do printf "HTTP/1.0 200 OK\r\nContent-Length: 12\r\n\r\nservice-vip\n" | nc -l -p 8080; done'\'' >/dev/null &&
 for i in $(seq 30); do [ "$(docker service ls --filter name=lab-service --format "{{.Replicas}}")" = 2/2 ] && exit 0; sleep 1; done
 docker service ps --no-trunc lab-service; docker service logs lab-service; exit 1'
check swarm_secret_config sh -c '
 printf secret-content | docker secret create lab-secret - >/dev/null &&
 printf config-content | docker config create lab-config - >/dev/null &&
 docker service create --detach --name lab-config-service --network lab-overlay --no-resolve-image --secret lab-secret --config lab-config harddind-base:test /bin/sleep 300 >/dev/null &&
 for i in $(seq 30); do id=$(docker ps -q --filter label=com.docker.swarm.service.name=lab-config-service)
 if [ -n "$id" ]; then docker exec "$id" /bin/sh -c '\''[ "$(cat /run/secrets/lab-secret)" = secret-content ] && [ "$(cat /lab-config)" = config-content ]'\'' && exit 0; fi; sleep 1; done; exit 1'
check swarm_vip sh -c '
 docker run --rm --network lab-overlay harddind-base:test /bin/sh -c '\''wget -qO- -T 5 http://lab-service:8080/ | grep -qx service-vip'\'''
check swarm_scale sh -c '
 docker service scale --detach lab-service=1 >/dev/null &&
 for i in $(seq 30); do [ "$(docker service ls --filter name=lab-service --format "{{.Replicas}}")" = 1/1 ] && exit 0; sleep 1; done; exit 1'
check swarm_cleanup sh -c '
 docker service rm lab-service lab-config-service >/dev/null &&
 for i in $(seq 20); do docker secret rm lab-secret && docker config rm lab-config && break; sleep 1; done &&
 docker swarm leave --force >/dev/null && [ "$(docker info -f "{{.Swarm.LocalNodeState}}")" = inactive ]'
# Must remain failing until the candidate can execute CUDA inside a container.
check gpu_fixture_import sh -c 'docker import /gpu-rootfs.tar harddind-gpu:test >/dev/null'
check gpu_compute docker run --rm --gpus all harddind-gpu:test /cuda-probe
printf 'HARDDIND_RESULT {"name":"external_network","status":"not_tested"}\n'
printf 'HARDDIND_RESULT {"name":"full_docker_compatibility","status":"not_tested"}\n'
[ "$fail" -eq 0 ]
