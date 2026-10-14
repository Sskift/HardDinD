# 网络逐包开销定位与批量收发实验

本轮继续在 Starpub 的现有无特权容器中运行原版 Docker。内核、基础磁盘和静态 echo 程序固定，外层不增加权限。新增 `--network-batch 1..64`，默认仍为 1；64 是未证明稳定加速的实验，不能作为性能达标结论。

## 改动与测试方法

网络后端增加实际 link recv/send/poll 调用计数。实验用 recvmmsg/sendmmsg 合并 Unix seqpacket 帧，每次最多 64 帧；保留逐帧边界、部分发送后的剩余队列、非阻塞重试与有界队列。它不合并应用系统调用或省略真实 Docker 网络栈。

`compare-network.py` 串行运行 1／64／64／1 四个独立实例，每种模式、每项负载各六个样本。在每个实例前及所有实例后运行原生对照，共 15 个原生样本。所有相关进程使用同一组 CPU 0–3，CPU 非独占，记录前后负载和 cgroup 压力。

每个样本传输并核验 32 MiB 每方向，分别测 64 KiB 请求／响应与持续双向传输。两边执行同一静态服务器，第二轮使用只含该二进制的确定性 tar 导入镜像，避免依赖 registry 的在线状态。tar 的哈希和各实例真实镜像 ID 都保留。另验证 1、1200、8000、60000 字节 UDP 数据报、TCP 流内容及 EOF、删除转发，以及正常关机；这些子集检查均通过。

CPU 数据来自实际进程／线程 schedstat。采样窗口包含读取计数的时间；guest 只累计两次快照都存在的线程，新增或消失的线程数量另记，因此它是定位辅助，不能当成无误差的应用 CPU 成本。

## 第二轮结果

`net-batch-v2.json` 是修正后的对照，`net-batch-v2-analysis.json` 由 `scripts/analyze-network.py` 生成。

| 负载 | 原生中位吞吐 | 单包模式 | 批量模式 | 批量相对单包 |
| --- | ---: | ---: | ---: | ---: |
| 64 KiB 请求／响应 | 8740.73 Mb/s | 445.54 Mb/s | 416.70 Mb/s | 下降 6.47% |
| 持续传输 | 12103.75 Mb/s | 594.95 Mb/s | 523.73 Mb/s | 下降 11.97% |

请求／响应的 link 收发调用中位数从 70838.5 次降到 5657 次，减少 92.01%；网络后端 CPU 从约 0.429 秒降到 0.358 秒。持续传输的调用从 67744.5 降到 5745，减少 91.52%，后端 CPU 从约 0.391 秒降到 0.327 秒。所有计时样本的后端丢包计数均为 0。

因此，逐包宿主调用确实占用 CPU，但减少调用并没有改善本轮吞吐。批量发送改变帧进入 guest 的时机、流水执行和调度，是后续需要拆分验证的候选原因；这次没有测到足以认定具体主因的证据。单包模式相对原生仍约有 19.62 倍、20.34 倍的吞吐差距，完整性能验收保持失败。

## 第一轮保留说明

`net-batch-v1.json` 中持续传输曾显示改善，而请求／响应回退。但代码检查发现单包路径也在反复初始化批量描述符数组，给基线增加了额外工作，因此没有采用该轮作为优化结论。第二轮恢复原来的单包接收循环，并将性能用镜像改为本地确定性导入。第一轮原始日志、源码和运行器保存于 `fixtures/` 及对应实例目录，不以第二轮覆盖。

第一轮和第二轮的基础程序一致，但镜像准备方式与网络后端源码不同；比较只能在各轮内部进行，不能直接把两轮中位数的变化全部归因于某个优化。

## 复现与状态

```sh
make network WORK=/root/harddind-rebuild
python3 scripts/compare-network.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/ipi-kernel-idle \
  --disk /root/harddind-rebuild/runtime-dns.ext4 --name NEW_UNIQUE_NAME
python3 scripts/analyze-network.py /root/harddind-rebuild/results/NEW_UNIQUE_NAME.json
```

实例名和结果文件不能复用。`--network-batch 64` 只能用于显式实验，默认配置不启用批量路径。本轮不新增自动端口发布、外层 bind 路径、IPv6 或 GPU 能力，也不替代此前联网 BuildKit、持久化等测试。

`net-batch-audit.json` 在所有计时结束后重新核对第二轮输入哈希，18 个相关进程组均无活进程或僵尸；现有 udock 只读健康检查返回 server=29.8.2、running=0、images=6。持久 COW 有意保留；审计脚本见 `fixtures/finalize-network-batch.py`。
