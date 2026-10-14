# Compose 多服务实测

Starpub 无特权外层容器中，固定 Compose 5.6.0 客户端通过实例专用回环端口驱动 guest 内的真实 Docker 29.8.2-harddind.1。使用固定 `memfd-kernel-v1`、独立 COW 磁盘和四个 guest CPU，启用已验证的 memfd RAM/stub；没有使用宿主 Docker socket。

`compose-v3` 的 **16 项检查全部通过**：固定镜像显式拉取、配置解析、实际 BuildKit 构建和执行、健康依赖启动、exec、服务名 HTTP 请求、卷重建保留、三副本 DNS 与逐副本 HTTP 内容校验、聚合日志、down、对象残留和正常关机。报告的 `full_acceptance` 仍为 false；这是新增功能覆盖，不是完整 Docker 或性能验收。

两次先前失败保留完整记录：

- `compose-v1`：构建通过，服务退出 127。固定 Alpine 的 BusyBox 不含 `httpd`；fixture 改用镜像自带 `nc -lk -e` 提供 HTTP，未新增未固定的软件包。
- `compose-v2`：多服务启动通过，验收脚本的 Python 3.10 无法解析 Docker 的纳秒时间戳。修正时间解析，改用真实 `health_status: healthy` 事件核对 web 启动顺序，避免依赖会滚动淘汰的五条健康日志。

三轮均正常关机，内核和基础磁盘哈希未变；第三轮每个命令进程组均正常清理，容器、网络、卷和镜像与初始快照相比没有新增残留。构建缓存容量不在该残留断言内。每轮目录含原始报告、命令日志、实例状态和实际执行的源码快照。

外层 bind mount、Compose secrets/configs/profiles/watch、Swarm stack、registry push/login、异常恢复和完整上游兼容性仍需验证。此处没有同机原生性能对照，也没有内部 CUDA 计算证据。

```sh
make compose-client WORK=/root/harddind-rebuild
python3 scripts/run-compose.py /root/harddind-rebuild \
  --name compose-new --kernel /path/to/immutable/kernel \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --dns 1.1.1.1 --pull-pinned --memfd-ram --memfd-stub
```

程序拒绝覆盖已有实例或结果；缺插件、缺镜像、失败和未测均不能算通过。
