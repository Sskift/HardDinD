# 当前运行边界与未解决的问题

目标仍然是在 Starpub 现有无特权容器内提供完整 Docker 功能、与原生一致的性能和 GPU。当前实现没有达到这个目标，也没有证据证明现有 UML 路线经过若干补丁就一定能达到。不能把继续开发的可能性写成交付保证。

这里实际修改的是独立的 Linux 7.2.10 UML 内核，它以宿主进程运行。Starpub 正在运行的宿主内核仍是 5.15.0-177-generic。编辑源码、编译 UML 和替换宿主正在运行的内核是不同操作；容器内的 root 不具有替换宿主内核所需的权限。

`run-host-probes.py` 以容器内 root 分别测试了 `unshare`、`clone`、`clone3`，每次尝试都在独立子进程中完成。普通 `clone` 可以创建进程，带 user/mount/network/PID/cgroup namespace 标志的 `clone` 返回 EPERM，`clone3` 返回 ENOSYS。`unshare` 同样被拒绝。`host-probes.json` 同时记录进程的 capability 和 seccomp 状态。不能把 `unshare` 失败直接推断成所有入口失败，因此这些入口是分别实测的。

Moby、containerd 和 runc 的源码可以修改，但删除它们的权限检查不会改变宿主系统调用的结果。获得正常 Docker API 响应，也不等于 namespace、文件系统、网络和资源隔离已经执行。

UML 能在自己的内核中实现 Linux namespace、cgroup、overlayfs 和网络，因此原版 dockerd 的一部分实际功能已经运行成功。这没有证明完整兼容，更没有证明性能一致。当前两个补丁只处理内核与用户进程之间的交接；它们没有跳过 guest 系统调用语义，也没有实现新的线程或 GPU 后端。

同一地址空间的线程串行化来自 `arch/um/kernel/skas/mmu.c` 的 `turnstile` 和 `arch/um/os-Linux/skas/process.c` 的用户态执行循环。删除锁会让不同线程同时修改同一份寄存器、信号栈和请求数据。正确实现需要独立线程状态、共享地址空间、TLB 更新同步、信号与退出处理；只把 `ncpus` 调大或删锁不能作为实现。

GPU 是另一项尚未实现的工程工作，不是因为外层没有显卡。外层四张 A100 已执行过真实 CUDA 计算；内层 `docker run --gpus all` 仍然失败。后端需要处理 NVIDIA ioctl 的内嵌指针和文件描述符、设备 mmap、UVM 与进程地址空间、锁页内存和退出清理。复制设备节点、导入驱动库镜像、在外层代跑测试都不能替代这些语义。

评估过的现成替代也不能直接声明达标：gVisor 提供 nvproxy，但它支持的驱动版本和设备接口有明确范围；其 Docker-in-gVisor 文档还要求关闭 dockerd 的 iptables/ip6tables 并调整网络配置。它可作为实现参考，不能直接替换完整 Docker 的目标。参见 [gVisor GPU 支持](https://gvisor.dev/docs/user_guide/gpu/) 和 [Docker in gVisor](https://gvisor.dev/docs/tutorials/docker-in-gvisor/)。

验收保持失败。当前资料支持的结论是：这些补丁可以继续研究和测量，但没有可交付的“完整功能、原生相同性能、GPU”实现。原生容器路线需要宿主实际允许所需的 namespace、挂载和资源控制；这超出当前固定运行条件。用户没有授权放宽目标，仓库也不把尚未实现或已失败的条件改成通过。
