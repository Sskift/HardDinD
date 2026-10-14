# HardDinD

当前目标：**在 Starpub 现有无特权 Docker 容器内支持几乎所有 Docker 命令，效率与同机原生相差不大，并提供可用 GPU。**

**重建中，尚未达到这三个目标。当前是可复现的内核开发和验收仓库，不能当成已完成的 Docker 替代品安装。**

旧版保存于 Git 标签 `archive/uml-6.6.158-20261014`。旧包装命令和安装器已移除。开发分支为 `codex/runtime-rebuild`。

## 当前实现

路线是**保留上游 Docker/containerd/runc，改造下面的 UML Linux 内核**。UML 内核作为 Starpub 外层容器中的普通进程运行，为内部 dockerd 提供 Linux 内核接口；目前没有 fork Moby，也没有修改 Starpub 的宿主内核。应用的 CPU 指令在宿主 CPU 上执行，系统调用与地址空间管理经过 UML，因此纯计算接近原生并不代表系统调用、I/O 或 GPU 同样接近原生。

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

进一步的 [执行开销定位](results/starpub-2026-10-14-profile/) 用独立计时和宿主追踪确认了逐页缺页、频繁信号屏蔽、权限变更重做 mmap，以及 fork/exec 重建执行进程的成本。报告给出具体函数、调用次数和优化顺序；本轮增加诊断能力，没有把候选优化或微基准结果计为完整性能验收。

新的 `defer_ipi=1` 实验保留宿主 IPI 信号投递，在 guest 关闭中断期间原子登记待处理向量，恢复时处理，减少反复调用宿主信号屏蔽。构建使用 `make kernel-ipi-experiment`，运行器使用 `--defer-ipi`；开关默认关闭。`scripts/compare-ipi.py` 对原生、上一版内核以及同一新内核的开关两种状态做交错对照，`scripts/run-ipi-probes.py` 使用实际补丁内的协议头进行异步信号压力测试。

该实验必须同时应用 `0009` 空闲等待修正：软件屏蔽不会把 IPI 留在宿主信号队列里，原来的 `signalfd` 等待因此可能漏唤醒。空闲路径现在在检查与睡眠期间屏蔽宿主 IPI，并检查软件 pending 位。原函数体的确定性复现与真实 guest 的零轮询回归见 [IPI 优化和故障定位](results/starpub-2026-10-14-ipi/)。单独应用 `0008` 的早期性能结果不能代表可用版本。

包含空闲修正的内核完成了八组串行交错对照：同一内核开关比较中，映射从 0.440 秒降到 0.221 秒，100 次 fork/exec 从 0.428 秒降到 0.188 秒，10 万次 getpid 基本不变。四线程系统调用本轮慢约 15%，仍需定位；映射、fork/exec、普通系统调用相对原生仍分别约 11.34、6.09、8.64 倍，不能认为整体性能已接近原生。

Docker 命令测试增加了暂停恢复、资源更新、attach、镜像提交与导出、事件流、网络连接切换、清理和单机 Swarm。Swarm 同时验证服务 VIP 的实际 HTTP 请求；所需 IPVS 与连接跟踪接口已加入内核配置。命令测试依然不等于完整产品验收，出站网络、外层端口与目录集成、GPU 和持续运行管理仍须继续完成。

新的 [内存权限同步实验](results/starpub-2026-10-14-protect/) 区分已存在映射的权限变化与物理页替换，前者用宿主 `mprotect`，后者保留 `mmap`。构建使用 `make kernel-protect-experiment`，运行器使用 `--mprotect-sync`，默认关闭。真实读写保护、丢弃页、固定地址替换、COW、fork 与文件截断回归通过；Docker 测试为 53 pass、1 GPU fail、2 not_tested。专项权限切换中位耗时下降约 24%，但第一次综合对照有严重慢样本，追加对照中的四线程系统调用仍有回退；完整性能没有达标，全部结果与失败版本均保留。

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
