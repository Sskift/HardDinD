# IPI 优化、空闲漏唤醒及 Docker 功能定位

本轮修改 UML 内核执行路径，并扩大原版 Docker 的命令效果测试。项目仍未达到功能、性能和 GPU 总目标。下文区分已复现的原因、实际修补和未解决问题；早期失败全部保留。

## 已确认的原因和修补

1. **频繁屏蔽宿主 IPI 信号。** 上一轮逐页缺页追踪显示大量 `rt_sigprocmask`。`0008` 让关闭 guest 中断期间的 IPI 先进入原子 pending 位，恢复中断后处理；保留原来的 Linux IPI 工作队列与 guest 系统调用语义。默认关闭，通过 `defer_ipi=1` 启用。
2. **上述改动暴露空闲漏唤醒。** 原 `os_idle_sleep` 假设关闭 guest 中断就已屏蔽宿主 IPI。软件 pending 接收到信号后，`signalfd` 没有待处理信号，而空闲路径也未检查软件 pending，于是可以带着工作进入无限 `poll`。`ipi-ipvs-spin0` 在并发页表回收中出现 RCU stall，宿主 vCPU 均在 `do_poll`，180 秒超时。`0009` 在检查到入睡期间屏蔽宿主 IPI，检查软件 pending，再恢复原信号掩码。
3. **Swarm VIP 缺少 IPVS。** 同一 `commands-v3` fixture 在未启用 IPVS 的内核上实际 HTTP 请求报 `Host is unreachable`，daemon 明确报告找不到 IPVS family；启用 IPVS 后请求通过。`swarm-without-ipvs` / `swarm-with-ipvs` 保留这一对照。内核同时补上连接跟踪 netlink 接口；未据此宣称多节点或 ingress 全部支持。上游 Docker 对 VIP/IPVS 的说明见 [官方网络文档](https://docs.docker.com/engine/swarm/networking/)。

空闲路径的回归直接编译修补前后的 `os_idle_sleep` 函数体，保留真实信号和 `signalfd/poll`，仅将无限等待限定为 50 ms 以记录漏唤醒。旧函数的两种通知时序均超时；新函数的 2,000 次通知没有超时。源码、函数体和协议头的哈希在 `ipi-idle-probes.json`。

修正内核 `ipi-kernel-idle` SHA256：`5d408030c303ce705d9edda224c1bccf206429bb56dc59c4cc3b04f071155922`。它包含 `0001`、`0003` 至 `0009`，以及 IPVS 配置。`ipi-idle-spin0-0/1/2` 三次真实 guest 回归均通过全部 5 组线程语义、18 项测量并正常关机，耗时分别约 15.5、14.1、12.3 秒。该回归覆盖了原先卡住的并发页表回收。

## 性能证据的边界

原生、上一版内核、新内核关闭 IPI 优化、开启 IPI 优化，按对称顺序串行运行；同一组宿主 CPU 0–3、同一静态程序，每项每模式 6 个样本。宿主 CPU 没有独占，微基准不能替代应用 I/O、Docker 构建、网络或 GPU 性能验收。

早期 `ipi-v1-comparison.json` 的映射与 fork/exec 提速有诊断价值，但其内核未包含空闲修正，不作为最终版本的性能结论。发现零轮询 RCU stall 后，`ipi-ipvs` 比较被中止；中止记录与已写出的样本保留，不能拼接成完整比较。

最终修正内核的完整结果见 `ipi-idle-comparison.json`。时间为每项全部工作的中位数，单位 ms；关闭/开启来自同一个内核二进制，仅切换 `defer_ipi`。

| 测试 | 原生 | 关闭优化 | 开启优化 | 关闭/开启 | 开启/原生 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10 万次 getpid | 48.803 | 421.074 | 421.578 | 1.00× | 8.64× |
| 100 次 fork + exec + wait | 30.829 | 428.344 | 187.799 | 2.28× | 6.09× |
| 512 轮 64 KiB 映射/触页/保护/释放 | 19.487 | 440.226 | 221.018 | 1.99× | 11.34× |
| 2,000 轮文件描述符操作 | 17.507 | 113.310 | 70.344 | 1.61× | 4.02× |
| 4 线程共 96 次 fork/exec | 12.809 | 324.319 | 273.901 | 1.18× | 21.38× |
| 4 线程共 8 万次身份系统调用 | 28.402 | 200.569 | 230.440 | 0.87× | 8.11× |
| 单线程纯计算 | 349.702 | 349.249 | 350.556 | 1.00× | 1.00× |
| 四线程纯计算 | 401.179 | 380.036 | 363.742 | 1.04× | 0.91× |

映射约快 1.99 倍、串行 fork/exec 约快 2.28 倍，普通 getpid 基本不变。四线程系统调用本轮反而慢约 15%，原因尚未拆分，不能写成全面加速。纯计算落在接近原生的量级；guest 的部分样本更快不能证明其优于原生，因为 CPU 未独占。所有模式各 6 个样本及原始值保存在 JSON 中。

最终同内核的两份追踪也完整通过。8 轮首次触碰 16 页时，宿主 `rt_sigprocmask` 从关闭优化的 **3,818 次降到 32 次**，而 SIGSEGV 仍各 128 次、宿主 mmap 为 128/132 次；这直接支持“消除了多数重复信号屏蔽，逐页映射成本仍在”的判断。开启优化后，8 对权限切换仍有 128 个宿主 mmap、0 个宿主 mprotect，提供了下一步分离权限更新的明确目标。计数包含相应阶段内的辅助工作与计时器活动，不能将其转换为时间占比。

追踪原文以 `ipi-idle-trace-on/off.strace.gz` 保存，结果见 `ipi-idle-trace-comparison.json`。`scripts/compare-profile-traces.py` 要求两边相同内核、程序、磁盘、CPU 集合与 spin 值，并扣除被 seccomp 拦截而未在宿主执行的 guest 调用；不比较被追踪的运行时间。

## 功能与失败记录

测试扩大为 55 项。它验证真实容器操作、镜像内容、资源配置和单机 Swarm 请求，不等于 55 个命令或完整 Docker 覆盖率，范围见 [命令覆盖说明](../../docs/command-coverage.md)。GPU 仍在内部执行检查，不能用外层 A100 计算成功替代。

最终修正内核的 `docker-ipi-idle.json` 为 **52 pass、1 GPU fail、2 not_tested**，55 项全部返回，约 207 秒结束；正常关机，无初始或最终残留进程。GPU 报错仍为 `failed to discover GPU vendor from CDI`，设备 ABI 后端没有实现。未测项为出站网络和完整兼容性。单次 Swarm 退出通过不关闭下面的历史偶发失败。

- `docker-ipi-final`：52 pass、1 GPU fail、2 not_tested，guest 退出后出现宿主残留进程，需要强制清理，因此 `finished=false`。当时清理器没有记录进程身份，不能确定原因。
- `docker-ipi-cleanup-audit`：正常关机，没有残留，51 pass、2 fail、2 not_tested。新增失败为 `docker swarm leave --force` 的 `context deadline exceeded`。没有复现残留不能证明修复；空闲漏唤醒与该超时之间也还没有因果对照。
- `docker-ipi-v1` 的网络连接测试误用了 `--network none` 后再连接 bridge；`docker-ipi-v2` 和 `docker-ipi-ipvs` 的服务误用了最小 BusyBox 不包含的 `httpd`。这些是测试 fixture 问题。`fixtures/` 保留三版脚本，最终 v3 使用已存在的 `nc`，并且校验实际 VIP HTTP 响应。
- `ipi-host-probes-output.log` 保留一次旧压力程序退出 1 的记录。当时没有失败计数，不能确定原因。增强诊断后的 `ipi-host-probes-v2.json` 在 1、4、8 个宿主 CPU 上分别处理 400,000 次真实信号和工作项，全部通过；另存的旧启动时序诊断没有复现原失败。
- 早于本轮的偶发宿主 child 丢失仍未定位；不因本轮其他测试成功而关闭。

## 下一步优化点

- **权限更新单独走 mprotect。** `arch/um/kernel/tlb.c:update_pte_range` 对 present 且需同步的 PTE 都走 mmap，连只改变权限也重建映射。应区分“同一宿主页仅改权限”和“新映射/更换物理页”，合并相邻权限范围；撤销写权限、COW 和 unmap 仍须同步完成。尚未实现。
- **降低首次触页与 fork/exec 的重复工作。** 当前仍逐页缺页、创建新 runner，再重建程序和栈映射。先优化映射，再研究可靠的干净执行上下文复用，覆盖 TLS、信号、FD、并发 exec 和退出。尚未实现。
- **系统调用入口成本。** SIGSYS、状态复制、跨进程交接仍存在，IPI 修补不会消除它们。需要测量寄存器/浮点状态复制以及不同交接类型的轮询成本；不能用缓存少数 syscall 返回值冒充普遍优化。[Linux seccomp 文档](https://docs.kernel.org/userspace-api/seccomp_filter.html) 定义了 TRAP 的 SIGSYS 语义。
- **独立功能缺口。** 出站网络、外层端口/目录集成、持久运行管理与 GPU 设备 ABI 仍未实现。GPU 需要 ioctl 指针/FD、映射和内存生命周期，单补 CDI 配置并不能提供计算能力。

## 复现入口

```sh
make kernel-ipi-experiment WORK=/root/harddind-rebuild
python3 scripts/run-ipi-idle-probes.py /root/harddind-rebuild --before /path/to/original/time.c
python3 scripts/run-core.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --threads-experiment --broker-cache --defer-ipi \
  --thread-semantics --disk /root/harddind-rebuild/thread.ext4 --spin 0 --result-name NEW_NAME
python3 scripts/compare-ipi.py /root/harddind-rebuild \
  --baseline /path/to/previous/kernel --kernel /path/to/immutable/kernel --name NEW_COMPARISON
```

所有实验在 `/root/harddind-rebuild` 的独立 COW 磁盘和进程组中执行。不要覆盖原始证据，也不要与性能测量同时编译。`0008` 的 pending/owner 协议压力测试没有覆盖空闲等待集成，必须同时运行 `0009` 的回归。

本机与 Starpub 的 `make check` 均通过 11 项检查和脚本语法检查。源码在临时副本中完成 `0007 → 0009`、`0008 → 0009` 的补丁往返，最终文件与构建 stamp 一致；内核、配置、磁盘、程序及运行器哈希见 `ipi-idle-provenance.json`。本机重算追踪分析与远端结果逐字节相同。

最终检查了本轮 32 个私有实验进程组，没有残留；原有 `/root/udock` 只读健康检查仍为 Docker 29.8.2、运行中容器 0、镜像 6，未安装或替换原服务。证据见 `ipi-idle-final-checks.json`。`make acceptance` 仍按预期拒绝通过，不把这些实验替代完整验收。
