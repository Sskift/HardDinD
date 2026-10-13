# HardDinD

目标：**在现有无特权 Docker 容器内，提供完整 Docker 功能、与原生一致的性能、可用 GPU。**

**重建中，尚未达到这三个目标。当前是可复现的内核开发和验收仓库，不能当成已完成的 Docker 替代品安装。**

旧版保存于 Git 标签 `archive/uml-6.6.158-20261014`。旧包装命令和安装器已移除。开发分支为 `codex/runtime-rebuild`。

## 当前实现

- 固定 Linux 7.2.10 源码和 SHA256，启用 UML seccomp、SMP，以及 Docker 所需的 namespace、cgroup、文件系统和网络功能。
- 第一个内核补丁：`seccomp_spin=0..10000`，在内核与用户进程交接时进行有上限的轮询，再退回 futex 睡眠。默认 0 保留上游行为。实验同时记录延迟和 CPU 消耗。
- 同一份静态程序分别测外层原生执行、ptrace、seccomp；区分多进程与同一进程多线程。
- 在独立 guest 中运行原版 Docker 29.8.2，执行功能测试。没有关闭 Docker 自身的 bridge、iptables 或 cgroup 来绕过失败。
- CUDA 测试实际分配显存、上传数据、运行 PTX、下载结果并校验全部元素。外层成功和内部 Docker 成功分开记录。
- 验收失败、缺测、跳过均不算通过。`make acceptance` 目前应返回非零。

## 已知未完成项

1. 系统调用和 fork/exec 的速度还未达到原生。
2. UML 仍将同一地址空间的用户态线程串行化；只增加 `ncpus` 不能解决。
3. GPU 设备 ABI、地址空间映射、显存生命周期和 Docker GPU 接入尚未实现。Star­pub 外层的 4 张 A100 已完成真实计算；不能据此声称内部支持 GPU。
4. 对外网络、外层目录共享语义和长期运行管理尚未重建；当前 Docker 测试只在隔离的实验 guest 内执行。
5. 上游 seccomp 模式存在内部隔离缺口，需要在产品化前解决。

具体机器证据位于 [results/starpub-2026-10-14](results/starpub-2026-10-14/)，总状态为 [results/acceptance.json](results/acceptance.json)。性能数据是微基准，不代表完整 Docker 或 GPU 性能。

## 开发与复现

内核构建和运行要求 Linux x86_64；需要 Python 3.9+、GCC、make、flex、bison、bc、libelf 开发文件、e2fsprogs、patch 和 tar。准备 Docker rootfs 需要容器内 root，以设置文件所有权和执行 chroot，不要求外层特权容器。GPU 测试镜像使用外层已安装的 NVIDIA `libcuda`、PTX JIT 库及其 glibc 依赖，另行记录哈希；这些库不纳入 Git，也不能代替 GPU 设备后端。

```sh
make check
make kernel WORK=/root/harddind-rebuild
make probe-root WORK=/root/harddind-rebuild
make native WORK=/root/harddind-rebuild
make core WORK=/root/harddind-rebuild
make docker-root WORK=/root/harddind-rebuild
make docker-test WORK=/root/harddind-rebuild
make acceptance
```

工作目录必须专用于实验。磁盘已存在时构建器拒绝覆盖；同一磁盘的测试必须串行执行。Star­pub 已有的 `/root/udock` 不会被这些脚本更新。

对照不同内核路径：

```sh
python3 scripts/run-core.py /root/harddind-rebuild --mode ptrace --cpus 1
python3 scripts/run-core.py /root/harddind-rebuild --mode seccomp --cpus 4 --spin 0
python3 scripts/run-core.py /root/harddind-rebuild --mode seccomp --cpus 4 --spin 256
python3 scripts/run-core.py /root/harddind-rebuild --mode seccomp --cpus 4 --spin 4096
```

每轮保存退出码、超时状态、原始日志、程序和内核哈希、测量值。只有出现完整的结束标记和全部预期数据才算实验成功。实验成功与项目验收通过是两个独立状态。

执行 `python3 scripts/summarize.py /root/harddind-rebuild/results` 汇总原生与 guest 微基准。汇总器检查两边程序哈希相同、各组数据完整，不将微基准自动判为完整性能验收通过。

## 开发方向

先解决 UML 执行核心的系统调用开销、同一地址空间的并行线程和 GPU ABI，再决定 Moby/containerd 需要哪些后端改造。当前没有把 Moby fork 或完整 GPU 后端伪装成已实现。

GPU 后端需要处理 NVIDIA 驱动 550.120 的 ioctl 内嵌指针/FD、mmap、UVM、锁页内存与进程退出。仅创建 `/dev/nvidia*`、转发 `nvidia-smi` 或在外层代跑计算，均不能满足目标。
