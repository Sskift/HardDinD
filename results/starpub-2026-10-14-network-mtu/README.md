# 虚拟链路 MTU 对照与联网回归

本轮新增可显式设置的 `--mtu 576..65520`，同时配置 libslirp、UML vec0、Docker 默认 bridge 和新建用户 bridge。Docker 仍是原版 29.8.2，内核仍是含 IPI 空闲修正的 7.2.10；没有修改 Starpub 外层网卡、权限或 `/root/udock`。默认仍为 1500。

9000 MTU 在本轮提高了请求／响应和连续传输吞吐，并通过 11 项联网与持久化检查。65520 的连续传输更快，但请求／响应明显回退。完整功能、原生性能和内部 GPU 均未达标。

## 改动为何需要贯穿整条路径

只改 vec0，Docker 的 veth/bridge 仍可能按 1500 分段。运行器现在把同一 MTU 传给网络后端、UML 的 vector 参数和 guest 初始化；dockerd 使用 `--mtu` 及 `--default-network-opt bridge=com.docker.network.driver.mtu=...`。后者只设置新网络的默认值，用户的显式网络选项仍有优先权。[Docker 官方说明](https://docs.docker.com/reference/cli/dockerd/#default-network-options)

这与上游 slirp4netns 使用较大虚拟 MTU 的方向一致，但没有把其其他机器的成绩当成本机结果。[上游用法与基准](https://github.com/rootless-containers/slirp4netns#benchmarks)

静态测试程序通过 SIOCGIFMTU 读取实际 vec0 和容器 eth0，分别验证默认 bridge、新建用户 bridge、显式覆盖为 1500 的网络。不是只检查配置字符串。非默认 MTU 的启动还要求 guest 确认该配置，避免用旧基础磁盘静默忽略参数。

## 六组交错对照

`net-mtu-v2.json` 顺序为 1500／9000／65520／65520／9000／1500；每种 MTU、每项负载六个样本，原生每项 21 个样本。每样本核验 32 MiB 每方向，分别使用 64 KiB 请求／响应与持续双向传输。原生和内部 Docker 使用相同静态程序与 CPU 0–3；CPU 非独占，宿主负载、cgroup 压力、逐包计数与 CPU 快照均保存。

| MTU / 路径 | 请求／响应中位吞吐 | 连续传输中位吞吐 |
| --- | ---: | ---: |
| 原生回环 | 8334.31 Mb/s | 10835.70 Mb/s |
| 1500 | 300.13 Mb/s | 322.25 Mb/s |
| 9000 | 361.27 Mb/s | 796.37 Mb/s |
| 65520 | 51.24 Mb/s | 1566.44 Mb/s |

相对本轮 1500，9000 分别提高约 20.4% 和 147.1%。连续传输的 link 收发调用中位数从 70441 降到 12392.5，网络后端 CPU 从约 0.449 秒降到 0.118 秒；包数量和处理成本确实降低。9000 的连续传输仍约为原生的 1/13.6，不能称为接近原生。

65520 在连续传输中达到 1500 的约 4.86 倍，但请求／响应大幅回退，且样本波动明显。TCP 分段、缓冲和确认等待是候选解释，尚未进行足以确定具体原因的隔离试验。不能删掉慢样本，也不能据此默认选择最大 MTU。

所有计时样本的网络后端丢包计数均为 0。CPU 快照包括采集窗口；guest 只累计两次快照都存在的线程，线程身份变化另有记录。原生回环与内部 Docker 的不同网络路径正是比较范围，不代表外部互联网或物理网卡吞吐。计算摘要见 `net-mtu-v2-analysis.json`。

## 功能与退出状态

六个实例均通过真实 MTU、用户覆盖、1–60000 字节 UDP 数据报、TCP 数据及 EOF、删除转发和正常关机检查。

随后 `network-mtu9000-v1.json` 在新实例上通过全部 11 项联网／持久化检查：固定摘要镜像冷拉取、容器 HTTPS、联网 BuildKit 下载与 SHA 校验、外层原版 CLI 的 stdin/TTY、显式 TCP/UDP 转发及删除、网络数据校验、重启后的镜像 ID、restart=always 容器和命名卷标记保留。仍未自动同步 `docker -p`；外层 bind 路径、IPv6 流量、完整生命周期、GPU 和上游兼容性覆盖继续开放。

更早的 `net-mtu-v1.json` 在第一个 1500 MTU 实例关机时失败：guest 已打印正常关机并返回 0，但清理器发现一个残留进程，随后进行了强制回收。它发生在 1500 基线上，不能据此认定由大 MTU 导致，也没有足够证据排除其他原因。当时读取 exe 失败会丢掉已读取的进程状态；`guest_cleanup.py` 现保留每个字段及独立错误码。清理等待时间和失败标准没有放宽，第一轮保持失败。后续成功不能关闭这个间歇性退出问题。

## 复现

```sh
make network WORK=/root/harddind-rebuild
bash scripts/make-runtime-root.sh /root/harddind-rebuild /root/harddind-rebuild/protect-docker-root runtime-mtu
python3 scripts/compare-network.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/ipi-kernel-idle \
  --disk /root/harddind-rebuild/runtime-mtu.ext4 --mtus 1500 9000 65520 --name NEW_COMPARISON
python3 scripts/run-runtime.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/ipi-kernel-idle \
  --disk /root/harddind-rebuild/runtime-mtu.ext4 --dns 1.1.1.1 --mtu 9000 --name NEW_RUNTIME
```

已有磁盘、实例和证据不覆盖；复现必须使用新名称。持久实例也可以使用同一组 kernel/disk/dns/mtu 参数，通过 `harddind.py ... start` 启动。用户配置不同 MTU 时，不能复用输入不匹配的旧实例。

`net-mtu-audit.json` 在计时结束后核对输入哈希，21 个相关进程组均无活进程或僵尸；现有 udock 的只读健康检查返回 server=29.8.2、running=0、images=6。原始退出失败、完整日志、每次关机报告和审计脚本均保留。COW 磁盘有意留在实验目录中，不纳入 Git。
