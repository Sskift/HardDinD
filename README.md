# HardDinD

当前目标：**在 Starpub 现有无特权 Docker 容器内支持几乎所有 Docker 命令，效率与同机原生相差不大，并提供可用 GPU。**

**重建中，尚未达到这三个目标。当前是可复现的内核开发和验收仓库，不能当成已完成的 Docker 替代品安装。**

旧版保存于 Git 标签 `archive/uml-6.6.158-20261014`。旧包装命令和安装器已移除。按用户要求，后续开发直接提交并推送到 `main`。

## 当前实现

路线是**保留上游 Docker/containerd/runc，改造下面的 UML Linux 内核，并为需要外层配合的功能增加 Moby 补丁**。UML 内核作为 Starpub 外层容器中的普通进程运行，为内部 dockerd 提供 Linux 内核接口；没有修改 Starpub 的宿主内核。应用的 CPU 指令在宿主 CPU 上执行，系统调用与地址空间管理经过 UML，因此纯计算接近原生并不代表系统调用、I/O 或 GPU 同样接近原生。

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
3. 完整 GPU 设备 ABI、地址空间映射、显存生命周期和 Docker GPU 接入尚未实现。已有实验性的 UML 控制查询通道；Star­pub 外层的 4 张 A100 已完成真实计算，但内部 CUDA 仍未通过。
4. IPv4 出站、显式 TCP/UDP 转发和正常关机重启后的持久化已在实验入口验证；可选的 Moby 补丁已验证 IPv4 自动 `docker -p` 子集。IPv6 流量、多个外层地址复用同一端口、外层目录共享语义、异常恢复与长期运行管理尚未完成。网络请求／响应性能仍明显低于原生。
5. 上游 seccomp 模式存在内部隔离缺口，需要在产品化前解决。

具体机器证据位于 [results/starpub-2026-10-14](results/starpub-2026-10-14/)，总状态为 [results/acceptance.json](results/acceptance.json)。性能数据是微基准，不代表完整 Docker 或 GPU 性能。

后续的 [交接协议实验与重复回归](results/starpub-2026-10-14-handoff/) 没有证明稳定加速，实验补丁未纳入默认构建。原生入口的实际限制、线程及 GPU 缺口见 [运行边界](docs/runtime-boundary.md)。目前没有验证过能同时满足三个目标的后端。

新的 [并行线程实验](results/starpub-2026-10-14-threads/) 在相同 4 个宿主 CPU 上做了串行 A/B/B/A 对照：四线程纯计算中位数从 2.584 秒降到 0.533 秒，原生为 0.449 秒；系统调用和 fork/exec 仍分别约为原生的 7.8 倍和 19.7 倍。不能据此认定整体性能达标。

后续的 [系统调用与进程创建优化](results/starpub-2026-10-14-performance/) 重新测量了原生和各版内核。在最后一轮相同 CPU 的交错对照中，10 万次系统调用从 0.557 秒降到 0.481 秒，100 次 fork/exec 从 0.783 秒降到 0.460 秒。四线程计算本轮接近原生，但系统调用、fork/exec、映射分别仍约为原生的 8.45、15.44、29.47 倍。早期对照中出现的崩溃和中间版本的性能回退均保留，不以重测覆盖。

进一步的 [执行开销定位](results/starpub-2026-10-14-profile/) 用独立计时和宿主追踪确认了逐页缺页、频繁信号屏蔽、权限变更重做 mmap，以及 fork/exec 重建执行进程的成本。报告给出具体函数、调用次数和优化顺序；本轮增加诊断能力，没有把候选优化或微基准结果计为完整性能验收。

新的 `defer_ipi=1` 实验保留宿主 IPI 信号投递，在 guest 关闭中断期间原子登记待处理向量，恢复时处理，减少反复调用宿主信号屏蔽。构建使用 `make kernel-ipi-experiment`，运行器使用 `--defer-ipi`；开关默认关闭。`scripts/compare-ipi.py` 对原生、上一版内核以及同一新内核的开关两种状态做交错对照，`scripts/run-ipi-probes.py` 使用实际补丁内的协议头进行异步信号压力测试。

该实验必须同时应用 `0009` 空闲等待修正：软件屏蔽不会把 IPI 留在宿主信号队列里，原来的 `signalfd` 等待因此可能漏唤醒。空闲路径现在在检查与睡眠期间屏蔽宿主 IPI，并检查软件 pending 位。原函数体的确定性复现与真实 guest 的零轮询回归见 [IPI 优化和故障定位](results/starpub-2026-10-14-ipi/)。单独应用 `0008` 的早期性能结果不能代表可用版本。

包含空闲修正的内核完成了八组串行交错对照：同一内核开关比较中，映射从 0.440 秒降到 0.221 秒，100 次 fork/exec 从 0.428 秒降到 0.188 秒，10 万次 getpid 基本不变。四线程系统调用本轮慢约 15%，仍需定位；映射、fork/exec、普通系统调用相对原生仍分别约 11.34、6.09、8.64 倍，不能认为整体性能已接近原生。

Docker 命令测试增加了暂停恢复、资源更新、attach、镜像提交与导出、事件流、网络连接切换、清理和单机 Swarm。Swarm 同时验证服务 VIP 的实际 HTTP 请求；所需 IPVS 与连接跟踪接口已加入内核配置。命令测试依然不等于完整产品验收，网络与目录集成、GPU 和持续运行管理仍须继续完成。

新的 [内存权限同步实验](results/starpub-2026-10-14-protect/) 区分已存在映射的权限变化与物理页替换，前者用宿主 `mprotect`，后者保留 `mmap`。构建使用 `make kernel-protect-experiment`，运行器使用 `--mprotect-sync`，默认关闭。真实读写保护、丢弃页、固定地址替换、COW、fork 与文件截断回归通过；Docker 测试为 53 pass、1 GPU fail、2 not_tested。专项权限切换中位耗时下降约 24%，但第一次综合对照有严重慢样本，追加对照中的四线程系统调用仍有回退；完整性能没有达标，全部结果与失败版本均保留。

## 开发与复现

共享目录后端正在实验：UML 内核增加 `VIRTIO_UML`、`FUSE_FS` 和 `VIRTIO_FS`，通过 vhost-user 连接外层独立的 virtiofsd，不依赖 KVM 或外层 `/dev/fuse`。源码固定 virtiofsd 1.13.2，`make virtiofs WORK=... RUST_TOOLCHAIN=...` 校验 Rust 1.98.1 的提交与架构，Cargo 使用上游锁文件，原生依赖解包到实验工作目录。`start --share /absolute/directory` 在 guest 同一路径挂载导出目录，随后可使用普通 Docker bind mount；每个目录有独立服务进程，实例退出时检查清理结果。

第一轮 `shared-auto-v1` 因上游服务的 `unshare(CLONE_FS)` 被外层 seccomp 拒绝而启动失败，Docker 侧 18 项均为缺测。`virtiofs/patches/0001` 改为分别保护临时工作目录和 umask，保留 4 个服务线程及普通 I/O 并发；两项 8 线程、各 256 次循环的并发检查通过。随后真实 Docker bind mount 在 `auto` 模式通过 8/18 项，在 `never` 模式通过 14/21 项（增加了非 root 通知与并发创建检查）。无缓存修复了普通读写和元数据陈旧问题，但共享 mmap、跨层文件锁、外层向内部的通知仍失败；未因此缩减目标。原生两项 root inotify 检查也失败，独立 UID 1001 对照可创建实例，未调整宿主配额。相同程序的读写吞吐仍明显低于原生，原始失败、原因定位、性能和清理证据见 [共享目录实测](results/starpub-2026-10-14-shares/README.md)。

共享目录实验默认 `--share-cache never`，以避免普通读写静默使用陈旧数据。需要 `MAP_SHARED` 的应用在当前导出目录上仍不能正常工作，后续必须补齐；`--share-cache auto` 仅保留为显式对照选项。未启用 `--share` 的实例不受这个选项影响。

`scripts/run-shared-locks.py --functional-only` 运行完整的锁功能检查和清理审计，跳过所有计时采样；报告保留 `benchmark_complete=false`，并独立记录 `functional_complete` 与各项成败。可用它在并行开发期间查找行为差异，性能测量仍须单独安排。此选项与 `--benchmark-only` 互斥，默认运行方式不变。

[POSIX 字节范围锁候选实测](results/starpub-2026-10-15-posix-candidate-v1/README.md) 为原生 37/37、内部 Docker 35/37。两个失败已定位为单字节范围被错误扩展到文件末尾，以及最大偏移常量少算一位；候选尚未合入正式补丁目录。原始失败与 99 个进程组的清理证据保留，修复及更完整的 PID、死锁、exec 语义验证仍在进行。

`virtiofs/patches/0002` 增加外层共享 `flock`：锁绑定到对应的宿主打开文件描述，阻塞请求由独立等待队列重试，处理 FUSE interrupt，避免等待者占满普通文件工作线程。运行入口传入 `--shared-flock`，服务拒绝与零线程池组合；锁状态迁移尚未实现，因此显式返回不支持。POSIX 字节范围锁尚未接入，未声明支持。[共享锁实测](results/starpub-2026-10-14-shared-locks/README.md) 的旧内核为原生 21 项通过、Docker 17 项通过及 4 项失败。取消失败定位到内核 virtiofs 的 interrupt 空实现；新增内核补丁 `0011` 的实际回归通过 SIGUSR1/SIGKILL 取消，Docker 达到 19/21，两项 POSIX 锁仍失败，实例正常关闭且没有残留。1000 次加锁/解锁微基准的 Docker 耗时在两轮中分别约为原生 163 倍和 290 倍；缺少交错内核对照，不能因功能修复宣称提速，性能仍未达标。

`make moby WORK=/root/harddind-rebuild` 构建固定提交 `8af9fe3a36bab3e039862a2ab1cef1880c9b4d03` 的 Docker 29.8.2，补丁保存在 `moby/patches/`，Go 1.26.8 工具链与源码均校验锁定的 SHA256，依赖仅使用上游 vendor。补丁增加独立的 `HARDDIND_PORT_DRIVER_STATE` 入口，复用上游同步端口分配协议，不开启 rootless 模式或改变 cgroup 行为。第二个补丁修复实测发现的失败回滚问题：端口冲突后的网络配置被删除，导致再次启动没有端口映射。

[自动端口发布实测](results/starpub-2026-10-14-published-ports/) 的 15 项检查通过，覆盖外层回环 TCP/UDP 数据校验、停止与删除后的释放、默认和自定义 bridge 冲突重试、多端口回滚、临时端口、范围、`-P`、四个并发发布与整个实例重启恢复。第一版冲突失败记录保留。控制请求经实例专用的串口通道传递，数据仍走原有 libslirp 路径；此次没有宣称性能提升。IPv6、不同外层地址复用同一端口、Swarm ingress 和异常恢复尚未通过验收。

```sh
make moby port-agent network WORK=/root/harddind-rebuild
bash scripts/make-runtime-root.sh /root/harddind-rebuild \
  /root/harddind-rebuild/protect-docker-root runtime-ports --with-ports
python3 scripts/harddind.py /root/harddind-rebuild ports-dev start \
  --kernel /path/to/immutable/ipi-idle-kernel \
  --disk /root/harddind-rebuild/runtime-ports.ext4 --dns 1.1.1.1 --publish-ports
# 使用该入口后，docker run -p 127.0.0.1:8080:80 会同步创建外层 IPv4 映射。
python3 scripts/harddind.py /root/harddind-rebuild ports-dev stop
```

`--with-ports` 安装自定义 dockerd/docker-proxy 与端口 agent；未指定时仍使用基础磁盘中的 daemon。`--publish-ports` 必须配合该磁盘，运行器会验证 daemon 确实连接端口驱动。未启用时保留下面的显式 `forward` 用法。

[网络与持久运行实测](results/starpub-2026-10-14-network/) 的 11 项检查全部通过：固定摘要镜像冷拉取、容器 HTTPS、真实联网 BuildKit 构建、外层原版 CLI 的 stdin/TTY、显式 TCP/UDP 转发及删除、镜像与容器及命名卷跨重启保留。前两轮 DNS 失败也完整保留；外层默认解析器的 AAAA 查询实测约 4 秒后返回 SERVFAIL，本轮通过 `--dns 1.1.1.1` 明确选择经探测可用的解析器，没有修改外层 DNS 或过滤 AAAA。相同静态程序的 64 KiB 请求／响应测试中，原生回环与内部 Docker 中位数分别约 9171、422 Mb/s；这是特定负载的约 21.75 倍差距，不能称为网络性能达标。

新的持久运行实验使用 `scripts/harddind.py`。它保存独立 COW 磁盘，启动网络后端和真实 dockerd，外层使用同版本的原版 Docker CLI；`stop` 通过 UML 管理通道请求 guest 正常关机。网络后端固定 libslirp 4.6.1，支持 IPv4 TCP/UDP、显式外层转发和上述可选自动发布。这个入口仍属实验，GPU、完整发布语义、外层 bind 路径和完整网络性能尚未完成。

进一步的 [网络收发定位](results/starpub-2026-10-14-network-batch/) 增加调用计数、CPU 记录和连续传输对照。`--network-batch 64` 将 link 收发调用减少约 92%，但修正后的对照吞吐反而回退约 6%–12%，因此默认仍为单包模式。实验只降低了部分 CPU 成本，没有解决整体网络速度差距。

新的 [MTU 对照](results/starpub-2026-10-14-network-mtu/) 允许 `--mtu 9000` 同步配置虚拟链路和 Docker bridge。在本轮六组交错对照中，相对 1500，请求／响应吞吐提高约 20%，连续传输提高约 147%；11 项联网与持久化回归全部通过。65520 出现请求／响应回退，默认 MTU 仍为 1500。使用该选项需要包含新初始化脚本的基础磁盘；报告也保留了一次 1500 基线关机后的残留进程失败。

```sh
make network WORK=/root/harddind-rebuild
make runtime-root WORK=/root/harddind-rebuild
python3 scripts/harddind.py /root/harddind-rebuild dev start --kernel /path/to/immutable/ipi-idle-kernel --dns 1.1.1.1
python3 scripts/harddind.py /root/harddind-rebuild dev docker version
python3 scripts/harddind.py /root/harddind-rebuild dev docker buildx version
# 若容器已在 guest 的 8080 端口发布服务，再显式映射到外层回环接口：
python3 scripts/harddind.py /root/harddind-rebuild dev forward add tcp 127.0.0.1 8080 8080
python3 scripts/harddind.py /root/harddind-rebuild dev stop
```

`runtime-root` 默认从干净的 `protect-docker-root` 复制，可直接调用其脚本传入其他已固定包版本的 Docker rootfs；第三个参数指定新的 fixture 名称，已有磁盘不会覆盖。实例镜像、容器和卷保存在 `WORK/instances/NAME/root.cow`；同名实例输入变化会拒绝启动。`--dns` 默认为经虚拟地址 `10.0.2.3` 使用外层解析器，上例显式选择其他解析器。API 仅转发到外层回环地址的动态端口，CLI 使用独立配置目录；未实现额外认证。`scripts/run-runtime.py` 从新实例测试镜像冷拉取、容器 HTTPS、联网构建、交互、TCP/UDP 显式转发、同程序网络对照和跨重启持久化，不把该测试子集计为完整验收。

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

继续解决 UML 执行核心的系统调用开销、同一地址空间的并行线程和 GPU ABI；Moby 仅按实际接口缺口增加可复现补丁。端口入口的源码改动不代表完整 Docker 功能或 GPU 后端已经实现。

GPU 后端需要处理 NVIDIA 驱动 550.120 的 ioctl 内嵌指针/FD、mmap、UVM、锁页内存与进程退出。仅创建 `/dev/nvidia*`、转发 `nvidia-smi` 或在外层代跑计算，均不能满足目标。

新的 [UML GPU 控制通道](results/starpub-2026-10-14-gpu-control/) 已在实际 guest 和内部 Docker 中完成真实 NVIDIA 版本查询、跨页参数、错误恢复、多线程及描述符生命周期检查。构建使用 `make kernel-gpu-experiment`，持久实例显式传入 `--gpu-control-experiment` 才会启用 `/dev/harddind-nvidiactl`。该节点仅开放控制查询，没有实现 CUDA、设备 mmap 或 `--gpus all` 支持；完整 GPU 验收保持失败。

后续 [GPU 描述符关联实测](results/starpub-2026-10-14-gpu-fd/) 将 NVIDIA `REGISTER_FD` 接入真实 UML 和内部 Docker，验证四设备关联、高编号描述符、引用生命周期、线程与独立进程。测试同时发现并修复 UML `copy_from_user` 越过 `PROT_NONE` 读取权限的问题；默认补丁 `0014` 用通用页权限检查解决，旧内核失败和新内核通过均保留。实验补丁 `0013` 仍只提供控制与设备关联子集，内部 CUDA 和设备 mmap 未实现；不代表 GPU 目标完成。

[可执行 memfd 后端实验](results/starpub-2026-10-14-memfd/) 增加两个默认关闭的独立选项：`--memfd-ram` 和 `--memfd-stub`，分别改变 UML 物理内存和用户进程启动程序的宿主文件后端。固定内核的四组合对照已完成线程／映射验证；部分微基准耗时下降，但仍明显落后于原生，宿主负载影响和完整 Docker/GPU 回归需要继续验证。运行器核对实际后端，不能仅因传入参数就判定启用成功。

[Compose 多服务实测](results/starpub-2026-10-14-compose/) 新增固定 Compose 5.6.0 的真实构建、健康依赖、服务解析、卷重建和三副本扩容覆盖，16 项检查通过并正常清理。前两轮 fixture 与时间解析失败均保留。该子集未覆盖完整 Compose、异常恢复、性能或 CUDA，完整验收状态不变。

[RM 客户端实验](results/starpub-2026-10-14-gpu-rm/) 增加固定 NVIDIA ABI 的根客户端分配与释放。原生前后对照为 29/29，guest 与内部 Docker 生命周期探针为 28/29；未开放类别的错误返回与原生不同，因此总验收仍失败。尚未实现设备 mmap、UVM、内部 CUDA 或显存生命周期，不计为 GPU 目标完成。

[RM_CONTROL 根查询实测](results/starpub-2026-10-14-gpu-root-control/) 接入四种带嵌套指针的固定查询。原生前后、guest 和五次内部 Docker 生命周期探针均为 17/17，四张 GPU 的设备信息与错误返回一致；Docker 外层 12 项检查通过。旧客户端的类别错误仍有一项不一致，设备映射、UVM、计算和传输尚未实现，完整 GPU 与性能验收状态不变。

[根查询指针边界回归](results/starpub-2026-10-14-gpu-pointer-errors/) 随后发现并修复两类越界指针的错误返回差异，旧失败保留。扩展探针在原生、guest 和五次内部 Docker 运行中均为 23/23，包含空地址、未映射地址、越界地址和无效客户端组合，仍不计作 CUDA 验收。

[真实 CUDA 设备发现实测](results/starpub-2026-10-14-gpu-device-numbers/) 发现用户态库会把动态杂项节点重建为 `195:255`，随后被容器设备权限拒绝。新增补丁 `0021` 在 guest 内注册 NVIDIA 兼容设备号，真实 CUDA 因而能打开控制节点、读取卡信息并分配根客户端；设备权限和生命周期回归通过。内部计算仍在带二级指针的驱动版本查询 `RM_CONTROL 0x101` 失败，原始轨迹及未完成状态保留。

[驱动版本二级指针实测](results/starpub-2026-10-14-gpu-build-version/) 用补丁 `0022` 接入该查询，30 项探针及 24 组原始结果在原生、guest 和五次内部 Docker 运行中一致，原有根查询与线程回归通过。完整 CUDA 实际通过 `0x101` 后，在下一条客户端继承共享策略 `0xd04` 失败；内部 GPU 计算、UVM、设备映射和接近原生性能仍未完成。

[GPU 可调度等待实测](results/starpub-2026-10-15-gpu-async/) 新增默认关闭的 `0017` 实验：通过完成 IRQ 让等待 ioctl 的任务进入 guest 调度器。相同人工延迟的同步／异步对照、worker 死亡唤醒、四 CPU 查询与内部 Docker 并发重启通过；真实 CUDA 仍在 `0xd04` 失败。使用 `make kernel-gpu-async-experiment` 构建，实例须显式传入 `--gpu-control-experiment --gpu-async-wait`。这不代表完整 GPU 或近原生性能达标。

[CUDA 客户端策略实测](results/starpub-2026-10-15-gpu-policy-trace/) 补充了 `0xd04` 的真实二级参数：原生两条 PID 策略的目标都是当前 RM 客户端句柄，内部第一条仍被拒绝。追踪器现在记录有界参数快照，原生前后四卡计算通过；这为后续身份语义实现提供输入证据，不代表内部 GPU 已完成。

[客户端继承共享策略与驱动错误语义](results/starpub-2026-10-15-gpu-client-policy/) 用 `0023`/`0025` 接入 `0xd04` 并保留真实驱动对尺寸和 flags 的判定。扩展探针在 guest 与五次内部 Docker 运行中均为 105/107 条结果匹配原生；剩余未知命令错误返回及继承 FD 两项差异。真实内部 CUDA 已通过两条策略调用，仍在 `0xcb33` 对象分配处失败。原始日志和探针已取回，完整 GPU 与性能验收保持未完成。

[固定参数对象分配实测](results/starpub-2026-10-15-gpu-fixed-alloc/) 用 `0026` 接入 `0xcb33` 的非 FINN 分配及权限掩码。guest 与五次实际 Docker 运行均有 56/57 条结果匹配原生，FINN 格式差异仍记为失败；线程和独立子进程分配/释放通过。真实 CUDA 已越过该分配，下一处阻断为 `RM_CONTROL 0x1f0`。原生前后四卡计算通过，内部计算仍未执行，完整功能与性能目标保持未完成。

[系统能力与分配错误顺序实测](results/starpub-2026-10-15-gpu-system-features/) 用 `0027` 消除 `0xcb33` 的 FINN 错误顺序差异，扩展的 74 条结果在 guest 与五次 Docker 运行中全部匹配原生。第二版同时修正首版遗漏的系统能力过滤条件，31 条查询边界结果在 guest 与五次 Docker 运行中全部匹配原生；两组 Docker 各 12 项外层检查通过。实际 CUDA 已通过 `0x1f0`，下一处阻断仍为缺失的 `/dev/nvidia-uvm`。第二版 87 项证据核对通过，记录的 22 个进程组均已清理，首版失败记录保留。内部 GPU 计算与近原生性能尚未完成。
