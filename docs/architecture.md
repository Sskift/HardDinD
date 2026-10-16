# 技术路线

## 外层条件（实测）

Starpub 容器：宿主内核 5.15，`CapEff=a80425fb`（Docker 默认能力集，无 SYS_ADMIN），seccomp 为 Docker 默认配置。user/mount/net/pid namespace 的 `unshare`/`clone` 返回 EPERM，`clone3` 返回 ENOSYS。没有 KVM，没有宿主 Docker socket，不能写宿主 cgroup。有 4 张 A100（驱动 550.120），72 核与其他租户共享。

rootless Docker/Podman、sysbox、fuse-overlayfs 都需要 user namespace 或 mount，不可用。PRoot/udocker 给不了网络、cgroup、overlay 语义。没有 KVM 的 QEMU 只能软件模拟。gVisor 跑 Docker 需关闭 iptables，nvproxy 不支持 550.120。剩下的可行路线是在外层以普通进程运行一个完整 Linux 内核：UML。

## 结构

```
外层容器（无特权）
├── linux（UML 内核，每个 vCPU 一个宿主执行进程，共享 guest 地址空间）
│   └── guest：tini → dockerd/containerd/runc → 容器
├── network-backend（libslirp，guest 出站 NAT、端口转发、发布端口）
├── virtiofsd（可选，外层目录共享）
└── 外层 docker CLI ──tcp 127.0.0.1:随机端口──> guest dockerd
```

应用的 CPU 指令直接在宿主 CPU 上执行。guest 的系统调用被外层 seccomp 过滤器以 SIGSYS 截获，交给 UML 内核处理后再返回；缺页和地址空间变化由映射管理进程在宿主上执行 mmap/mprotect。所以纯计算接近原生，系统调用、缺页、fork/exec、网络的开销集中在这条交接路径上。

guest 的上游 DNS：外层有可路由的解析器就用外层的；只有回环解析器时（Starpub 是 `127.0.0.11`，AAAA 查询约 4 秒后 SERVFAIL），按原生 Docker 对仅回环宿主的做法改用公共解析器 1.1.1.1、8.8.8.8。

## 已知瓶颈（重构前的测量，原生 = 1）

| 项目 | guest/原生耗时 |
| --- | ---: |
| 1/4 线程计算 | 1.0 |
| getpid | 8.5 |
| fork/exec | 15–22 |
| mmap/触页/mprotect | 29 |
| clock_gettime / sched_getcpu | 约 200（vDSO 仍转系统调用） |
| 网络吞吐 | 原生的 1/13–1/20（libslirp 单线程用户态 TCP） |

## 推进顺序

1. **时钟 vDSO**（已完成，补丁 0031，默认开启）：guest 内核以宿主不变 TSC 为时钟源，发布只读 vvar，容器所在的 time namespace 也走快路径；`clock_gettime` 从约 5 µs 降到约 25 ns（原生约 18 ns），`micro.clock` 从 0.006 到约 0.6。guest 内核每 100 ms 以宿主时钟为参考校正频率和相位（`harddind_clock_sync=1`，默认开），600 秒长测中 MONOTONIC/REALTIME 与宿主的偏差上界约 0.85 µs，未校正时为 24 ms（约 16 ppm）；宿主 REALTIME 跳变会传播到 guest。`sched_getcpu` 经 rseq（`CONFIG_UML_X86_RSEQ`，补丁 0029）从约 6 µs 降到约 1.7 ns（原生 1.6 ns）。rseq 会选上 `SCHED_MM_CID`，exec/exit 时调用 `irq_work_sync()`；UML 原先没有 IRQ-work 中断，该函数每次退化为等一个 RCU 宽限期（约 43 ms），fork+exec 因此从约 2 ms 变成约 85 ms。补丁 0029 现在用专门的可合并 IPI 投递 IRQ work（`harddind_irq_work=0` 可恢复旧行为对照），rseq 重新开启，fork+exec 与关闭 rseq 时持平。
2. **并行执行稳定化**（0003 起）：并发 fork/exec 下曾丢失宿主进程，根因未证实修复。
3. **缺页批量映射**（0036/0037、0040–0045，默认开启）：连续匿名写缺页的窗口从 32 页增长到最多 512 页，并受 VMA、PMD 和内存余量约束；窗口内排序物理页、合并后预填充宿主映射，保留映射身份以合并保护变更，并省去重复 unmap 和最终地址空间清理。guest 页分配、清零、memcg 记账和 COW 语义不变；整块 RAM 预填充和缺页 profiling 仍为默认关闭的候选。同次交替 A/B 的 `mem.touch` 为原生 0.38→0.86，B/A 2.20（1.91–2.34）。
4. **系统调用改写**：参考 gVisor systrap，把 `mov sysno,%eax; syscall` 改写为跳到 trampoline，省掉信号投递和 sigreturn。
5. **网络后端**（进行中）：guest 网卡与后端之间用 vnet 头传 GSO 大段和校验和卸载（补丁 0034），slirp 直接生成 TCP GSO 段，发布端口吞吐 0.17→0.65、请求响应 0.20→0.36、出站 0.03→0.18。容器之间的流量完全在 UML 内核里，受执行核心限制。空闲 vCPU 原先共用一个 signalfd 等待队列，每次网络中断把所有空闲 vCPU 都唤醒（出站每 GiB 约 2 万次上下文切换）；补丁 0039 改为每线程等待，补丁 0038 让阻塞的发送等可写中断而不是反复调度 NAPI，出站 0.22→0.28、bridge 0.32→0.52、发布端口请求响应 0.26→0.38（ci net-idle-txpoll）。剩余瓶颈：slirp 后端单线程约 0.66 ms/MB（上限约 1.5 GB/s），guest 出站集中在一个 vCPU 上。下一步评估 passt：它可在运行时增删转发（pesto），但缺 IPv4 分片重组、沙箱依赖 namespace，需要补丁。
6. **GPU**（次要）：控制通道和映射已在四卡上完成真实计算与校验；还缺 `--gpus all` 接入、并发上下文、显存生命周期和性能。辅助进程以 CLONE_VM 共享 guest 地址空间，ioctl 内嵌指针天然有效；NVIDIA ABI 可参考 gVisor nvproxy 中相邻驱动版本的定义。

共享目录默认使用 virtiofsd `thread_pool_size=0`、小同步回复 inline completion 和最多 1024 次 LOOKUP/GETATTR 等待轮询（0046–0048）；请求 profiling 保持关闭。交替 A/B 的 bind metadata 加速 2.10 倍（2.04–2.32），bind write 为 0.93 倍（0.92–0.98），该取舍经用户接受。外层 mmap 写入没有通知的一致性缺口仍存在。

## 安全边界

上游 UML seccomp stub 存在内部隔离缺口：容器内恶意进程可能逃出 UML，落到外层容器的同一用户。这不影响 Starpub 宿主，只影响 guest 内容器之间的隔离。多租户使用前需要修复。
