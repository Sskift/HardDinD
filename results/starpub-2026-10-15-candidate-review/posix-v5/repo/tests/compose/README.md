# Compose 验收 fixture

`compose.yaml` 由 `scripts/run-compose.py` 驱动，针对运行在 UML guest 内的真实
dockerd（29.8.2-harddind.1）执行真实 Compose 工作流。Compose 客户端是固定版本
v5.6.0（见 `sources.lock.json` 的 `docker_compose`），在 Starpub 外层通过回环
TCP 驱动内部 daemon；被编排的容器、网络、命名卷和 BuildKit 构建全部发生在
guest 内部。

## 检查顺序

1. `compose_plugin` — 插件存在、sha256 与 lock 一致、`compose version` 与固定版本一致。
2. `clean_start` — 工程名 `harddind-compose` 无残留容器、网络、卷。
3. `pinned_image_local` — 基础镜像（默认 lock 中 digest 固定的 alpine）已在本地 daemon；拒绝可变标签。
   显式 `--pull-pinned` 时增加 `pinned_image_pull`，只允许拉取该 digest。
4. `compose_config` — `compose config --format json` 解析：服务集合、全部 `pull_policy: never`、
   无 bind 挂载、`build.pull: false`、命名卷声明。
5. `compose_build` — 离线 BuildKit 构建 `builder` 并 `compose run --rm` 校验产物。
6. `compose_up` — `up -d --pull never db web worker`，全部 running。
7. `health_dependency` — `web` 必须在 `db` 健康检查首次通过后才启动（用实际时间戳验证，而不是只看配置）。
8. `compose_exec` — `compose exec -T web cat ...` 返回容器内文件内容。
9. `service_dns` — 从 `web` 经服务名 `http://db:8080/` 发起真实 HTTP 请求。
10. `volume_recreate` — 写入命名卷标记，`up --force-recreate` 后容器 ID 全变而数据保留。
11. `scale_workers` — `worker` 扩到 3 副本；验证服务 DNS 包含三个副本地址，再分别请求并校验每个副本的主机名。
12. `compose_logs` — 聚合日志包含三个服务的启动标记。
13. `down_cleanup` — `down --volumes --remove-orphans` 后无工程容器/网络/卷。
14. `daemon_leftovers` — 与运行前快照比较，镜像/容器/网络/卷无新增残留。
15. `clean_shutdown` — guest 正常关机、无强制回收。

缺插件、镜像缺失（且未显式 `--pull-pinned`）或任一步失败：相关项保持
`not_tested`/`fail`，报告非零退出，不能 pass。

## 明确不在范围

- guest 内部安装 Compose CLI（固定 Alpine 包清单不含 compose 插件）。
- 外层路径 bind mount（语义尚未完成，见共享目录实验）。
- registry push/登录、bake、watch、secrets/configs、profiles、Swarm stack、滚动更新。
- 外部网络与性能基准。

## 运行（由主代理串行调度）

```sh
bash scripts/install-compose-client.sh /root/harddind-rebuild
python3 scripts/run-compose.py /root/harddind-rebuild --name compose-v2 \
  --kernel /path/to/immutable/kernel --disk /root/harddind-rebuild/runtime.ext4
# 若 daemon 内尚无固定镜像，可显式 opt-in 仅拉取 lock 中的 digest：
#   --pull-pinned
```
