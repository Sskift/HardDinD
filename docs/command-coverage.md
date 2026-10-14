# Docker 命令覆盖与交付缺口

目标是用户要求的“几乎所有 Docker 命令可用、效率与同机原生相差不大”，并保留 GPU。验收应区分命令是否执行了预期操作、操作范围，以及运行性能。测试项数量不等于命令覆盖率。

目前使用固定 Docker 29.8.2、containerd 2.3.6、runc 1.4.3。主测试是 `tests/docker-suite.sh`，运行在独立 UML guest 内的原版 Docker daemon；真实应用进程、镜像、文件系统、网络与 cgroup 均在该 guest 中执行。

| 类别 | 当前测试执行的操作 | 仍需补齐的范围 |
| --- | --- | --- |
| 容器生命周期 | run/create/start/stop/restart/kill/rm、退出码、inspect/rename、wait、暂停恢复；持久 daemon 正常关机重启、restart=always 恢复 | 异常退出、强制关机恢复与长期并发压力 |
| 交互与进程 | stdin、TTY、attach、exec、logs、top、stats、信号、线程与并发 fork | 持续交互、终端 resize/detach、多会话与更广应用兼容性 |
| 镜像与构建 | import/save/load、commit/diff/history、export/import、tag/rmi、多阶段 BuildKit 与 cache mount；ECR 固定摘要冷拉取、联网 RUN 下载与 SHA 校验 | push、登录与凭据隔离、registry/manifest、更多 buildx/bake 场景；Docker Hub 外层直连超时尚未解决 |
| 存储 | 容器间共享命名卷、guest bind mount、tmpfs、只读文件系统、docker cp、按标签清理；镜像、容器与命名卷跨 guest 正常重启保留 | 外层路径的正确映射、异常恢复、I/O 性能 |
| 应用内存语义 | 容器内执行权限读写检查、MADV_DONTNEED、同地址文件替换、COW、fork、文件截断触发 SIGBUS | 更广应用、多线程并发撤销映射、长期压力与内部隔离 |
| 资源与权限 | memory OOM、pids 限制、动态写入 CPU/memory/pids 配置、guest seccomp、特权挂载、NET_ADMIN、user namespace | CPU 配额的实测限制效果、更多 device/cgroup 控制、内部隔离审计 |
| 单机网络 | bridge/DNS、网络连接断开、IPv6 地址、guest 内 published port/port inspect；IPv4 HTTPS、显式外层 TCP/UDP 转发与删除；MTU 配置及用户覆盖；可选补丁的 IPv4 自动发布、冲突回滚、并发发布与正常重启恢复 | IPv6 流量、不同外层地址复用同一端口、多种网络驱动、异常恢复；已测负载仍明显慢于原生 |
| 管理 | events、context、system df、container/volume prune；外层原版 CLI 连接持久 daemon，stdin/TTY 和 buildx 入口 | 完整 system/builder 管理、多客户端与生命周期压力 |
| Swarm | 单管理节点、service 副本、secret/config、scale/cleanup、overlay 上实际 VIP HTTP 请求 | 多节点、ingress 路由网格、stack、滚动更新和故障恢复 |
| GPU | 保留真实 CUDA 计算失败检查 | NVIDIA ABI 后端、内部计算/传输校验、多 GPU、内存生命周期与性能 |
| 扩展 | 尚未专门验证 | Compose、plugin、checkpoint/restore 及其他上游兼容项 |

每个结果文件记录各项 pass/fail/not_tested；是否实际通过，应以对应内核和 fixture 的原始报告为准。这个表描述测试范围，不把未运行的场景声明为可用。

最新内存权限实验的 56 个检查返回 53 pass、1 GPU fail、2 not_tested，guest 正常退出。新增项目是容器内真实内存映射语义；没有增加 GPU、出站网络或完整兼容性覆盖。证据见 `results/starpub-2026-10-14-protect/docker-protect-v3.json`。

独立网络与持久运行测试使用含 IPI 空闲修正的内核，未启用存在多线程回退的 mprotect 实验。11 项检查全部通过，包含真实联网构建、显式 TCP/UDP 转发和正常重启后的数据校验。默认外层 DNS 的失败与后续显式解析器配置均保留。证据见 `results/starpub-2026-10-14-network/network-v3.json`；这不是对上述所有命令的一次完整重测，也不是性能或 GPU 验收通过。

MTU 9000 的新运行也通过这 11 项检查，并验证默认 bridge、新建用户 bridge 的实际接口 MTU，以及用户显式覆盖为 1500。32 MiB 连续传输对照有所改善，整体效率仍未达标。一次较早的 MTU 1500 基线关机出现残留进程强制回收，原因未关闭；见 `results/starpub-2026-10-14-network-mtu/`。

自动发布使用固定 Moby 源码补丁与独立串口控制通道，`run-published-ports.py` 的 15 项检查在外层回环地址执行实际 TCP/UDP 校验。端口已占用时必须报错，并验证释放占用后重新启动同一容器仍恢复映射；多端口部分成功时须回滚，不能接管手工映射。此子集通过不代表 IPv6、多个外层地址、Swarm ingress 或完整网络性能通过。第一版丢失网络配置的失败和修补版记录见 `results/starpub-2026-10-14-published-ports/`。

`run-docker.py --checks REGEX` 可以在定位问题时只运行特定项目，例如 `^(image_import|swarm_)`。未选择的项目保留为 `not_tested`，这种运行始终不能通过完整验收。运行器同时核对 guest 内测试脚本 SHA256，避免拿旧磁盘的结果解释新的测试代码。

最近一次性能优化仍有系统调用、fork/exec 和映射差距。下一阶段必须同时推进实际命令入口、联网/镜像操作和性能；不能用纯计算接近原生代替完整效率判断，也不能把软件中的成功状态代替实际 GPU 计算。
