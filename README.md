# HardDinD

目标：**在现有无特权 Docker 容器内，提供完整 Docker 功能、与原生一致的性能、可用 GPU。**

**重建中，尚未达到这三个目标。当前是可复现的内核开发和验收仓库，不能当成已完成的 Docker 替代品安装。**

旧版保存于 Git 标签 `archive/uml-6.6.158-20261014`。旧包装命令和安装器已移除。开发分支为 `codex/runtime-rebuild`。

## 当前实现

- 固定 Linux 7.2.10 源码和 SHA256，启用 UML seccomp、SMP，以及 Docker 所需的 namespace、cgroup、文件系统和网络功能。
- 第一个内核补丁：`seccomp_spin=0..10000`，在内核与用户进程交接时进行有上限的轮询，再退回 futex 睡眠。默认 0 不主动轮询。实验同时记录延迟和 CPU 消耗。
- 另有实验补丁：在交接状态字中登记睡眠者，只唤醒已经准备睡眠的一端；用原子交接避免漏唤醒。实测没有稳定加速，因此保留在 `kernel/experiments/`，**不纳入默认构建**。
- 新的并行线程实验：`seccomp_threads=1` 为每个虚拟 CPU 建立独立宿主执行进程，以 `CLONE_VM` 共享地址空间；独立保存信号栈、寄存器和 TLS。映射操作交给固定进程串行处理，撤销映射或写权限在返回前同步生效。实验未纳入默认构建。
- 在并行路径上继续试验按需唤醒、空映射同步快路径，以及 `seccomp_broker_cache=1`：清除全部旧映射后，最多复用 4 个映射管理进程。应用执行进程仍然销毁、重建；没有缓存系统调用返回值或跳过 Docker 的检查。
- 映射管理进程保留同一个物理内存 FD，减少每次缺页时的 `sendmsg/recvmsg/close`；执行进程在进入应用前关闭它。接收 FD 时恢复控制缓冲区容量并检查截断或数量不符；并行路径以外继续使用原来的传递方式。
- 同一份静态程序分别测外层原生执行、ptrace、seccomp；区分多进程与同一进程多线程。
- 在独立 guest 中运行原版 Docker 29.8.2，执行功能测试。没有关闭 Docker 自身的 bridge、iptables 或 cgroup 来绕过失败。
- CUDA 测试实际分配显存、上传数据、运行 PTX、下载结果并校验全部元素。外层成功和内部 Docker 成功分开记录。
- 验收失败、缺测、跳过均不算通过。`make acceptance` 目前应返回非零。

## 已知未完成项

1. 系统调用和 fork/exec 的速度还未达到原生。
2. 默认路径仍串行执行同一地址空间的线程。并行实验已通过线程正确性和 Docker 内的初步回归，但尚缺长期压力、异常退出与内部隔离验证。旧路径还复现了并发 fork/COW 缺陷；扩大测试后，上一版并行内核也曾在并发 fork/exec 中丢失宿主进程并崩溃。
3. GPU 设备 ABI、地址空间映射、显存生命周期和 Docker GPU 接入尚未实现。Star­pub 外层的 4 张 A100 已完成真实计算；不能据此声称内部支持 GPU。
4. 对外网络、外层目录共享语义和长期运行管理尚未重建；当前 Docker 测试只在隔离的实验 guest 内执行。
5. 上游 seccomp 模式存在内部隔离缺口，需要在产品化前解决。

具体机器证据位于 [results/starpub-2026-10-14](results/starpub-2026-10-14/)，总状态为 [results/acceptance.json](results/acceptance.json)。性能数据是微基准，不代表完整 Docker 或 GPU 性能。

后续的 [交接协议实验与重复回归](results/starpub-2026-10-14-handoff/) 没有证明稳定加速，实验补丁未纳入默认构建。原生入口的实际限制、线程及 GPU 缺口见 [运行边界](docs/runtime-boundary.md)。目前没有验证过能同时满足三个目标的后端。

新的 [并行线程实验](results/starpub-2026-10-14-threads/) 在相同 4 个宿主 CPU 上做了串行 A/B/B/A 对照：四线程纯计算中位数从 2.584 秒降到 0.533 秒，原生为 0.449 秒；系统调用和 fork/exec 仍分别约为原生的 7.8 倍和 19.7 倍。不能据此认定整体性能达标。

后续的 [系统调用与进程创建优化](results/starpub-2026-10-14-performance/) 重新测量了原生和各版内核。在最后一轮相同 CPU 的交错对照中，10 万次系统调用从 0.557 秒降到 0.481 秒，100 次 fork/exec 从 0.783 秒降到 0.460 秒。四线程计算本轮接近原生，但系统调用、fork/exec、映射分别仍约为原生的 8.45、15.44、29.47 倍。早期对照中出现的崩溃和中间版本的性能回退均保留，不以重测覆盖。

## 开发与复现

内核构建和运行要求 Linux x86_64；需要 Python 3.9+、GCC、make、flex、bison、bc、libelf 开发文件、e2fsprogs、util-linux（flock）、patch 和 tar。准备 Docker rootfs 需要容器内 root，以设置文件所有权和执行 chroot，不要求外层特权容器。GPU 测试镜像使用外层已安装的 NVIDIA `libcuda`、PTX JIT 库及其 glibc 依赖，另行记录哈希；这些库不纳入 Git，也不能代替 GPU 设备后端。

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

工作目录必须专用于实验。磁盘已存在时构建器拒绝覆盖；运行器在基础磁盘上创建独立 COW 层，结束后删除该临时层，避免上一轮的网络、容器和卷污染下一轮。基础磁盘应由构建器生成并保持干净。性能对照必须串行执行。Star­pub 已有的 `/root/udock` 不会被这些脚本更新。

内核补丁按顺序在上游源码副本上验证，再更新已识别的源码文件。重复构建支持重叠补丁；遇到无法识别的本地修改会停止，避免覆盖开发中的改动。

对照不同内核路径：

```sh
python3 scripts/run-core.py /root/harddind-rebuild --mode ptrace --cpus 1
python3 scripts/run-core.py /root/harddind-rebuild --mode seccomp --cpus 4 --spin 0
python3 scripts/run-core.py /root/harddind-rebuild --mode seccomp --cpus 4 --spin 256
python3 scripts/run-core.py /root/harddind-rebuild --mode seccomp --cpus 4 --spin 4096
```

每轮保存退出码、超时状态、原始日志、程序和内核哈希、测量值。只有出现完整的结束标记和全部预期数据才算实验成功。实验成功与项目验收通过是两个独立状态。

执行 `python3 scripts/summarize.py /root/harddind-rebuild/results` 汇总原生与 guest 微基准。汇总器检查两边程序哈希相同、各组数据完整，不将微基准自动判为完整性能验收通过。

实验构建使用 `make kernel-handoff-experiment WORK=...`，之后运行 `make host-probes WORK=...`。它直接测试 `unshare`、`clone`、`clone3`，并使用补丁内同一个协议头文件进行 60 万次线程／进程交接校验。执行 `python3 scripts/compare-handoff.py WORK --before PATH_TO_PREVIOUS_KERNEL`，按 before/after/after/before 顺序运行三档轮询配置。再次执行 `make kernel` 会恢复默认补丁集；源码中的未知本地修改会阻止恢复。

并行线程实验单独构建为 `WORK/thread-build/linux`，不会覆盖 `WORK/kernel-build/linux`：

```sh
make kernel-threads-experiment WORK=/root/harddind-rebuild
make thread-root WORK=/root/harddind-rebuild
make thread-compare WORK=/root/harddind-rebuild
python3 scripts/run-core.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/thread-build/linux --threads-experiment \
  --thread-semantics --disk /root/harddind-rebuild/thread.ext4
python3 scripts/run-docker.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/thread-build/linux --threads-experiment \
  --disk /root/harddind-rebuild/docker.ext4 --timeout 360
```

`thread-root` 依赖先生成的 `probe-root`。Docker 磁盘须用本版构建器重新生成，才能包含新增的线程检查。`thread-compare` 对原生和两条执行路径使用同一份程序和同一组 CPU，按原生／旧路径／并行／并行／旧路径／原生顺序执行；它只比较微基准。线程语义检查独立执行，旧路径的失败记录不会被微基准成功覆盖。运行中的内核不能被重新构建替换，运行器会将哈希变化判为失败。

继续测系统调用和进程创建：

```sh
# 每次实验构建都使用 thread-build；对照前先复制、保留各版内核。
make kernel-threads-experiment WORK=/root/harddind-rebuild
cp /root/harddind-rebuild/thread-build/linux /root/harddind-rebuild/before-kernel
make kernel-dispatch-experiment WORK=/root/harddind-rebuild
cp /root/harddind-rebuild/thread-build/linux /root/harddind-rebuild/handoff-kernel
make kernel-performance-experiment WORK=/root/harddind-rebuild
cp /root/harddind-rebuild/thread-build/linux /root/harddind-rebuild/after-kernel
make dispatch-root WORK=/root/harddind-rebuild
python3 scripts/compare-dispatch.py /root/harddind-rebuild \
  --before /root/harddind-rebuild/before-kernel \
  --handoff /root/harddind-rebuild/handoff-kernel \
  --after /root/harddind-rebuild/after-kernel --name performance
bash scripts/make-dispatch-docker-root.sh /root/harddind-rebuild
python3 scripts/run-docker.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/after-kernel --threads-experiment --broker-cache \
  --disk /root/harddind-rebuild/dispatch-docker.ext4 --result-name docker-performance --timeout 360
```

`dispatch-root` 依赖 `thread-root`；`make-dispatch-docker-root.sh` 只接受已准备好的干净 Docker rootfs，第二个参数可指定它的位置。新增测试涵盖身份查询、文件读写、映射与权限、四线程系统调用、跨地址空间残留检查和并发 fork/exec。每种模式每项 6 个样本，顺序为原生／旧并行／按需唤醒／全部优化／全部优化／按需唤醒／旧并行／原生；失败运行保留日志，但不参与性能中位数。不完整的比较返回非零。新增 Docker 检查使用同一份二进制在实际容器内重跑这 6 项。

构建与测试共用文件锁，避免编译负载混入测量。配置先在临时目录解析，内容不变时保留现有 `.config`，避免仅因重新生成配置而触发全量编译。以上实验都不修改默认内核补丁集。

## 开发方向

先解决 UML 执行核心的系统调用开销、同一地址空间的并行线程和 GPU ABI，再决定 Moby/containerd 需要哪些后端改造。当前没有把 Moby fork 或完整 GPU 后端伪装成已实现。

GPU 后端需要处理 NVIDIA 驱动 550.120 的 ioctl 内嵌指针/FD、mmap、UVM、锁页内存与进程退出。仅创建 `/dev/nvidia*`、转发 `nvidia-smi` 或在外层代跑计算，均不能满足目标。
