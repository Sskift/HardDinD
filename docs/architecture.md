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

1. **时钟 vDSO**（已完成，补丁 0031，默认开启）：guest 内核以宿主不变 TSC 为时钟源，发布只读 vvar，容器所在的 time namespace 也走快路径；`clock_gettime` 从约 5 µs 降到约 25 ns（原生约 18 ns），`micro.clock` 从 0.006 到约 0.6。语义差别：guest 时钟不再跟随宿主 NTP 调频，长期运行会按 TSC 校准误差漂移；需要补上以宿主时间为参考的频率校正。
2. **并行执行稳定化**（0003 起）：并发 fork/exec 下曾丢失宿主进程，根因未证实修复。
3. **缺页批量映射**：`kernel/candidates/anon-faultaround.patch`，降低 fork/exec 和 mmap 开销。
4. **系统调用改写**：参考 gVisor systrap，把 `mov sysno,%eax; syscall` 改写为跳到 trampoline，省掉信号投递和 sigreturn。
5. **网络后端**：评估经 vhost-user 接入的 passt，替换 libslirp。
6. **GPU**（次要）：控制通道和映射已在四卡上完成真实计算与校验；还缺 `--gpus all` 接入、并发上下文、显存生命周期和性能。辅助进程以 CLONE_VM 共享 guest 地址空间，ioctl 内嵌指针天然有效；NVIDIA ABI 可参考 gVisor nvproxy 中相邻驱动版本的定义。

## 安全边界

上游 UML seccomp stub 存在内部隔离缺口：容器内恶意进程可能逃出 UML，落到外层容器的同一用户。这不影响 Starpub 宿主，只影响 guest 内容器之间的隔离。多租户使用前需要修复。
