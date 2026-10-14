# Docker 命令覆盖与交付缺口

目标是用户要求的“几乎所有 Docker 命令可用、效率与同机原生相差不大”，并保留 GPU。验收应区分命令是否执行了预期操作、操作范围，以及运行性能。测试项数量不等于命令覆盖率。

目前使用固定 Docker 29.8.2、containerd 2.3.6、runc 1.4.3。主测试是 `tests/docker-suite.sh`，运行在独立 UML guest 内的原版 Docker daemon；真实应用进程、镜像、文件系统、网络与 cgroup 均在该 guest 中执行。

| 类别 | 当前测试执行的操作 | 仍需补齐的范围 |
| --- | --- | --- |
| 容器生命周期 | run/create/start/stop/restart/kill/rm、退出码、inspect/rename、wait、暂停恢复 | 持久 daemon 管理、guest 重启后的恢复、异常退出与长期并发压力 |
| 交互与进程 | stdin、TTY、attach、exec、logs、top、stats、信号、线程与并发 fork | 持续交互、终端 resize/detach、多会话与更广应用兼容性 |
| 镜像与构建 | import/save/load、commit/diff/history、export/import、tag/rmi、多阶段 BuildKit 与 cache mount | pull/push、登录与凭据隔离、registry/manifest、更多 buildx/bake 场景 |
| 存储 | 容器间共享命名卷、guest bind mount、tmpfs、只读文件系统、docker cp、按标签清理 | 外层路径的正确映射、跨 guest 重启持久化、I/O 性能 |
| 资源与权限 | memory OOM、pids 限制、动态写入 CPU/memory/pids 配置、guest seccomp、特权挂载、NET_ADMIN、user namespace | CPU 配额的实测限制效果、更多 device/cgroup 控制、内部隔离审计 |
| 单机网络 | bridge/DNS、网络连接断开、IPv6 地址、guest 内 published port/port inspect | 出站网络、外层访问端口、IPv6 流量、多种网络驱动、吞吐与时延 |
| 管理 | events、context、system df、container/volume prune | 完整 system/builder 管理、CLI 的持久使用入口 |
| Swarm | 单管理节点、service 副本、secret/config、scale/cleanup、overlay 上实际 VIP HTTP 请求 | 多节点、ingress 路由网格、stack、滚动更新和故障恢复 |
| GPU | 保留真实 CUDA 计算失败检查 | NVIDIA ABI 后端、内部计算/传输校验、多 GPU、内存生命周期与性能 |
| 扩展 | 尚未专门验证 | Compose、plugin、checkpoint/restore 及其他上游兼容项 |

每个结果文件记录各项 pass/fail/not_tested；是否实际通过，应以对应内核和 fixture 的原始报告为准。这个表描述测试范围，不把未运行的场景声明为可用。

`run-docker.py --checks REGEX` 可以在定位问题时只运行特定项目，例如 `^(image_import|swarm_)`。未选择的项目保留为 `not_tested`，这种运行始终不能通过完整验收。运行器同时核对 guest 内测试脚本 SHA256，避免拿旧磁盘的结果解释新的测试代码。

最近一次性能优化仍有系统调用、fork/exec 和映射差距。下一阶段必须同时推进实际命令入口、联网/镜像操作和性能；不能用纯计算接近原生代替完整效率判断，也不能把软件中的成功状态代替实际 GPU 计算。
