# Docker 命令覆盖与交付缺口

目标是用户要求的“几乎所有 Docker 命令可用、效率与同机原生相差不大”，并保留 GPU。验收应区分命令是否执行了预期操作、操作范围，以及运行性能。测试项数量不等于命令覆盖率。

目前使用固定 Docker 29.8.2、containerd 2.3.6、runc 1.4.3。主测试是 `tests/docker-suite.sh`，运行在独立 UML guest 内的原版 Docker daemon；真实应用进程、镜像、文件系统、网络与 cgroup 均在该 guest 中执行。

## 当前优先级

按用户最新要求，先保证非 GPU 的 Docker 命令功能与性能一致，GPU 实现放到次要位置。未约定新的性能容差；GPU 保留在最终目标中，但不再作为近期实验的主线。

1. **执行核心与资源控制。** 优先审查已有的 CPU 时钟/调度、浮点状态和匿名缺页候选，分别隔离开销，再做实际内核对照。系统调用、fork/exec 和映射仍有明显差距；[CPU 时钟忙负载对照](../results/starpub-2026-10-15-candidate-review/cpu-clock-limits-v1/README.md) 中开启组的部分长采样窗口缩短，但五组整体验证仍无法判定，不能通过迁出限额组绕过。[实际工作量的内部复验](../results/starpub-2026-10-15-candidate-review/cpu-clock-work-v1/README.md) 已完成五组 35 个容器负载和前后原生参照；工作量校验一致，但带高频时钟与 CPU 查询的负载仅交付原生约 2.3%–5.3% 的工作量，CPU 时间统计不能替代执行量。时钟候选仍未证明稳定收益。[匿名缺页候选的进一步对照](../results/starpub-2026-10-15-candidate-review/cpu-faultaround-v3/README.md) 没有证明移除关闭状态的时钟读取后稳定加速，回退不能只归因于计时代码，内存语义仍须验证。
2. **存储和网络。** 继续完成外层共享目录的 mmap、锁、截断和多进程一致性，测量实际读写与元数据操作；网络同时检查请求延迟、吞吐、连接并发和发布端口。不得以关闭网络、资源限制或减少 CPU 数量换取通过。
3. **实际 Docker 工作流。** 在上述优化前后，对 `run/start/exec/stop`、镜像与构建、卷和文件复制、联网服务、Compose、资源更新等命令记录完整操作耗时与实际结果，补齐未测命令。新增 [工作流计时入口](workflow-timing.md) 已对生命周期、镜像往返、卷与 cp、构建缓存及 DNS/HTTP 分别完成三次成功实测，保留修复前失败；[版本化原始记录](../results/starpub-2026-10-15-workflow-timing/README.md) 尚无匹配原生 Docker 对照，Compose 计时也未覆盖。微基准用于定位；命令成功率与端到端性能分别报告，不能用一类替代另一类。

开发和重型验证继续在 Starpub 的独立目录进行。由主代理统一安排性能实验，避免并行构建、GPU 实验或其他测量争抢资源；保留相同输入、固定版本、原生前后参照、原始失败与清理记录。SSH 中断期间只能更新本地安排，远端代理是否已收到新优先级、已有实验是否结束都必须重新读取确认。

## 已有测试范围

| 类别 | 当前测试执行的操作 | 仍需补齐的范围 |
| --- | --- | --- |
| 容器生命周期 | run/create/start/stop/restart/kill/rm、退出码、inspect/rename、wait、暂停恢复；持久 daemon 正常关机重启、restart=always 恢复 | 异常退出、强制关机恢复与长期并发压力 |
| 交互与进程 | stdin、TTY、attach、exec、logs、top、stats、信号、线程与并发 fork | 持续交互、终端 resize/detach、多会话与更广应用兼容性 |
| 镜像与构建 | import/save/load、commit/diff/history、export/import、tag/rmi、多阶段 BuildKit 与 cache mount；ECR 固定摘要冷拉取、联网 RUN 下载与 SHA 校验 | push、登录与凭据隔离、registry/manifest、更多 buildx/bake 场景；Docker Hub 外层直连超时尚未解决 |
| 存储 | 容器间共享命名卷、guest bind mount、tmpfs、只读文件系统、docker cp、按标签清理；镜像、容器与命名卷跨 guest 正常重启保留 | 外层路径的正确映射、异常恢复、I/O 性能 |
| 应用内存语义 | 容器内执行权限读写检查、MADV_DONTNEED、同地址文件替换、COW、fork、文件截断触发 SIGBUS | 更广应用、多线程并发撤销映射、长期压力与内部隔离 |
| 资源与权限 | memory OOM、pids 限制、动态写入 CPU/memory/pids 配置、guest seccomp、特权挂载、NET_ADMIN、user namespace | CPU 限制实测：`--cpus` 节流有效；补齐内核控制器后 cpuset 已实际限制线程亲和性与运行 CPU，整组验收仍有计量开销造成的无法判定项。CPU shares、更多 device/cgroup 控制、内部隔离审计 |
| 单机网络 | bridge/DNS、网络连接断开、IPv6 地址、guest 内 published port/port inspect；IPv4 HTTPS、显式外层 TCP/UDP 转发与删除；MTU 配置及用户覆盖；可选补丁的 IPv4 自动发布、冲突回滚、并发发布与正常重启恢复 | IPv6 流量、不同外层地址复用同一端口、多种网络驱动、异常恢复；已测负载仍明显慢于原生 |
| 管理 | events、context、system df、container/volume prune；外层原版 CLI 连接持久 daemon，stdin/TTY 和 buildx 入口 | 完整 system/builder 管理、多客户端与生命周期压力 |
| Swarm | 单管理节点、service 副本、secret/config、scale/cleanup、overlay 上实际 VIP HTTP 请求 | 多节点、ingress 路由网格、stack、滚动更新和故障恢复 |
| GPU | 保留真实 CUDA 计算失败检查；独立控制通道的 `--device`、查询和权限检查通过 | 完整 NVIDIA ABI 后端、内部计算/传输校验、多 GPU、内存生命周期与性能 |
| 扩展 | 固定 Compose 5.6.0 多服务配置、构建、健康依赖、exec、服务 DNS/HTTP、卷重建、三副本、日志与清理，独立实验 16/16 | Compose secrets/configs/profiles/watch、plugin、checkpoint/restore 及其他上游兼容项 |

每个结果文件记录各项 pass/fail/not_tested；是否实际通过，应以对应内核和 fixture 的原始报告为准。这个表描述测试范围，不把未运行的场景声明为可用。

最新独立候选运行中，[CPU shares v4](../results/starpub-2026-10-15-candidate-review/cpu-shares-v4/README.md) 的首阶段实际分配符合 100/59 权重；反转阶段存在采样空档，等权阶段有效窗口不足，整轮仍无法判定。[共享 POSIX 锁 v5](../results/starpub-2026-10-15-candidate-review/posix-v5/README.md) 原生 46/46、内部 Docker 45/46，exec 测试依赖了镜像中不存在的程序；修复后 [v6](../results/starpub-2026-10-15-candidate-review/posix-v6/README.md) 实际达到原生和内部 Docker 各 47/47，正常关机且 133 个记录的进程组为空，源码已纳入主线。[GPU 生命周期原生 v4](../results/starpub-2026-10-15-candidate-review/gpu-lifecycle-v4/README.md) 暴露探针的 context 释放顺序、子进程参数及 SIGPIPE 错误；修复后的 [v5](../results/starpub-2026-10-15-candidate-review/gpu-lifecycle-v5/README.md) 已在四卡原生实际通过 136 次计算/传输/校验循环及多线程、多进程生命周期检查，可用作后续内部对照，但内部生命周期仍未通过。这些失败和无法判定项均保留，不计入完成。

[GPU 映射候选 v10](../results/starpub-2026-10-15-candidate-review/gpu-mapping-v10/README.md) 已通过实际四卡 NUMA 查询和原有 31/31、74/74 控制接口回归。内部 CUDA 仍在初始化失败：未知 `0x20810108` 控制布局和把字符设备事件标识误当成 eventfd 的实现正在修复。原生前后四卡计算通过仅用于对照；该候选没有内部计算或性能通过结论。

后续独立证据还包括 [Compose 多服务](../results/starpub-2026-10-14-compose/README.md)、[GPU 系统能力与分配边界](../results/starpub-2026-10-15-gpu-system-features/README.md) 和 [POSIX 范围修复候选](../results/starpub-2026-10-15-posix-candidate-v2/README.md)。GPU 控制接口通过不代表内部计算可用；锁候选第二次运行完成原生和内部 Docker 各 38 项现有检查，但完整 PID、死锁、exec 等语义仍未完成，首次启动超时的失败也保留；后续 v6 补齐本轮 PID、guest 死锁和真实 exec 检查后才合入，混合外层/guest 死锁与跨 PID namespace 映射仍未完成。这些运行使用各自记录的内核和输入，不能合并成当前版本的一次完整验收。

最新内存权限实验的 56 个检查返回 53 pass、1 GPU fail、2 not_tested，guest 正常退出。新增项目是容器内真实内存映射语义；没有增加 GPU、出站网络或完整兼容性覆盖。证据见 `results/starpub-2026-10-14-protect/docker-protect-v3.json`。

独立网络与持久运行测试使用含 IPI 空闲修正的内核，未启用存在多线程回退的 mprotect 实验。11 项检查全部通过，包含真实联网构建、显式 TCP/UDP 转发和正常重启后的数据校验。默认外层 DNS 的失败与后续显式解析器配置均保留。证据见 `results/starpub-2026-10-14-network/network-v3.json`；这不是对上述所有命令的一次完整重测，也不是性能或 GPU 验收通过。

MTU 9000 的新运行也通过这 11 项检查，并验证默认 bridge、新建用户 bridge 的实际接口 MTU，以及用户显式覆盖为 1500。32 MiB 连续传输对照有所改善，整体效率仍未达标。一次较早的 MTU 1500 基线关机出现残留进程强制回收，原因未关闭；见 `results/starpub-2026-10-14-network-mtu/`。

自动发布使用固定 Moby 源码补丁与独立串口控制通道，`run-published-ports.py` 的 15 项检查在外层回环地址执行实际 TCP/UDP 校验。端口已占用时必须报错，并验证释放占用后重新启动同一容器仍恢复映射；多端口部分成功时须回滚，不能接管手工映射。此子集通过不代表 IPv6、多个外层地址、Swarm ingress 或完整网络性能通过。第一版丢失网络配置的失败和修补版记录见 `results/starpub-2026-10-14-published-ports/`。

该自定义 Moby 二进制也完成未启用端口驱动的命令套件：52 pass、2 fail、2 not_tested。失败包括内部 GPU 和版本号写死的 context 断言；后者改为核对实际 daemon ID 后定向重测通过，其余 55 项保留 not_tested。原失败报告保留，不合并伪装成一次完整通过。

`run-docker.py --checks REGEX` 可以在定位问题时只运行特定项目，例如 `^(image_import|swarm_)`。未选择的项目保留为 `not_tested`，这种运行始终不能通过完整验收。运行器同时核对 guest 内测试脚本 SHA256，避免拿旧磁盘的结果解释新的测试代码。

最近一次性能优化仍有系统调用、fork/exec 和映射差距。下一阶段必须同时推进实际命令入口、联网/镜像操作和性能；不能用纯计算接近原生代替完整效率判断，也不能把软件中的成功状态代替实际 GPU 计算。
