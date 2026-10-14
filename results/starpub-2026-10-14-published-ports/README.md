# 自动 Docker 端口发布实验

`publish-ports-v2` 的 15 项检查通过；完整项目仍未验收通过。本轮没有性能对照，也没有内部 GPU 成功证据。

保留真实 Docker 29.8.2 daemon，在固定源码上增加独立端口驱动入口。guest agent 将 RootlessKit 端口协议通过实例专用串口转发给外层 supervisor，supervisor 同步调用 libslirp 的创建/删除接口。没有开启 rootless 模式，没有获得宿主 namespace、KVM、Docker socket 或额外 cgroup 权限。外层 `/root/udock` 没有修改。

实际验证：固定 TCP/UDP 的数据与分片校验，CLI inspect/port 与真实地址一致，stop/start/rm 释放与恢复，外层占用后的错误和重试，自定义 bridge 的别名保留，多端口部分失败回滚，手工映射不被接管，临时端口、端口范围、`-P`、四个并发发布，以及 restart=always 容器跨整个 guest 重启后在原端口收发数据。测试绑定外层 127.0.0.1。

第一版 `publish-ports-v1` 在冲突重试时失败：首次 start 正确报告 host port unavailable，但失败回滚删除了 `NetworkSettings.Networks` 中的 bridge；第二次 start 返回成功，`NetworkSettings.Ports` 却为空。HostConfig 中的端口请求仍存在。`0002-retain-network-on-start-failure.patch` 保留失败连接的原配置，让后续 start 重试；默认 bridge、自定义 bridge、多端口回滚分别回归通过。失败结果没有覆盖或计为通过。

同一自定义 Moby 二进制另跑现有 Docker 命令套件（此套件没有启用外层端口驱动）：`docker-moby-ports-v2` 为 52 pass、2 fail、2 not_tested，56 项完整返回、guest 正常退出。失败为内部 GPU 计算，以及 context 测试将版本字符串写死为 `29.8.2`，与补丁版本 `29.8.2-harddind.1` 不符。context 检查改成比较真实 daemon ID 后，`docker-moby-context-v1` 的定向重测通过；其余 55 项保持 not_tested，不能把定向运行当成完整重测。默认 seccomp、OOM/pids 限制、动态资源更新、映射语义、BuildKit 和单机 Swarm 等在前一完整运行中通过。外层端口驱动启用时的覆盖范围仍以上面的 15 项为准。

`results/*.json` 是检查结果，`results/*.log` 包含真实 CLI 命令与输出；`instances/` 保存每轮 guest/network 日志、请求响应和退出清理记录。`build-v1.log`、`build-v2.log` 保存 fixture 中的二进制哈希；内核、基础盘和脚本哈希也记录在结果中。关机后端口驱动无剩余映射，guest 与 helper 进程组清空。

`cleanup-audit.json` 再次检查已保存实例与本次命令回归的进程组、活动 supervisor 和现有 udock 服务状态；没有残留实验实例，现有服务仍可响应。

仍未完成：IPv6 发布，不同外层 IP 上相同端口的独立映射，临时端口遇到外层冲突时重新分配，guest 本机地址与外层地址的完整语义，Swarm ingress，多实例竞争和异常退出恢复。端口上限仍受现有 helper 的 256 条转发限制（含 API）。数据传输经过原来的网络路径；不能把自动建立映射视为性能达标。

复现命令（新实例名和新证据路径，已有结果会拒绝覆盖）：

```sh
python3 scripts/run-published-ports.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/ipi-kernel-idle \
  --disk /root/harddind-rebuild/runtime-ports-v2.ext4 --name publish-ports-v2
```
