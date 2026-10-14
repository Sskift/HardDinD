# UML memfd 内存与启动程序后端

**完整 Docker、接近原生性能和内部 GPU 目标仍未通过。** 本轮默认补丁 `0015` 提供两个独立、默认关闭的运行开关：`memfd_ram=1` 将 UML 物理内存文件改成共享可执行 memfd；`memfd_stub=1` 要求用户进程启动程序保存在只读封印的 memfd 中。

上游启动程序路径原本调用 `memfd_create(MFD_EXEC | MFD_CLOEXEC | MFD_ALLOW_SEALING)`，Star­pub 的 Linux 5.15 不支持该新标志而退回 ext4 临时文件。新路径只在 EINVAL 时重试旧接口；权限拒绝不会触发这个重试。启动程序写入完成后检查全部封印，阻止继续写入和改变大小。RAM memfd 仍保持可写，实际检查可执行映射，不改变外层权限、seccomp、AppArmor 或挂载配置。官方接口语义见 [Linux memfd 文档](https://docs.kernel.org/userspace-api/mfd_noexec.html)。

两个开关与原 GPU、SMP、cgroup、网络和磁盘功能独立。运行器记录实际 RAM 分配与启动程序后端，并拒绝把只有命令行参数、没有内核确认的运行算作已启用；持久实例保存配置，重启或复用时核对。没有默认启用这两个实验选项。

## 构建与首次实测

第一次编译因引用了当前源文件未声明的 `fatal()` 失败，保留失败补丁和日志；改为明确输出错误并退出，第二次编译和 stub 页外重定位检查通过。

内核 SHA256：`d872149b71ff0026201bebe5a8f482bb883a3c6c47c56d02d4411e444766bc5a`。

`memfd-smoke-v1` 在两个开关均启用时完成 3 组映射语义、5 组线程语义和 36 个样本，退出正常，无强制清理或残留。日志确认物理内存与启动程序均使用 memfd。可写映射、保护、COW、进程创建及多线程不是通过关闭功能来绕过的。

## 四组合对照

`memfd-factor-v1` 使用同一份不可变内核与磁盘、相同程序、CPU 0–3、4 vCPU、2 GiB 请求内存、spin=256、broker cache 和 deferred IPI。顺序为原生／file／ram／stub／both／both／stub／ram／file／原生；各模式各项六个样本，共 360 个测量值。八次 guest 均完成映射和线程检查，正常退出，无失败样本被排除。

下表为本轮中位数，单位毫秒。file 是两个开关都关闭，ram/stub 是单独启用，both 是同时启用。

| 负载 | 原生 | file | ram | stub | both | both / 原生 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 万次 getpid | 78.828 | 478.721 | 417.913 | 424.211 | 386.769 | 4.91× |
| 100 次 fork/exec/wait | 60.021 | 245.758 | 174.439 | 281.447 | 168.969 | 2.82× |
| 512 轮映射/触页/保护/释放 | 21.249 | 306.993 | 352.004 | 410.250 | 173.993 | 8.19× |
| 四线程 8 万次身份系统调用 | 32.835 | 274.993 | 285.878 | 451.908 | 212.288 | 6.47× |
| 四线程 96 次 fork/exec | 31.117 | 434.695 | 419.287 | 367.546 | 251.667 | 8.09× |

both 相对 file 的串行 fork/exec 耗时下降约 31.2%，映射下降约 43.3%。但单独启用的某些组合变慢，纯计算和宿主状态也有波动；CPU 没有独占，不能把该轮结果直接认定为稳定的因果收益。UML 的 ASLR/exec-shield 间隙导致日志中的物理文件长度随启动变化，实际分配长度已记录，不能把文件长度直接当作 guest 可用内存。

原始日志、每个样本、资源状态和源码哈希都保存；原始比较器快照为 `memfd-factor-v1-compare.py`。后续逐次原生对照与 GPU 回归结果见下文。微基准仍有明显原生差距，不构成项目性能验收。

## 复现

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
# 复制成功构建的内核到独立路径，避免运行中重建。
python3 scripts/compare-memfd.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name new-memfd-factor
python3 scripts/compare-memfd.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name new-memfd-bracket --pairwise
```

持久运行器和 core/Docker/GPU 测试入口接受 `--memfd-ram`、`--memfd-stub`。内部 CUDA、GPU 映射、完整共享目录和网络性能等原有缺口仍在范围内。

## 交错原生复测：收益仍不稳定

`memfd-bracket-v1` 顺序为原生／file／原生／both／原生／both／原生／file／原生。四次 guest 均通过全部语义和 36 个样本，正常退出；每个 guest 项目用前后两次原生中位数的平均值归一化，再比较 file 与 both。原生每项 15 个样本，file/both 每项各 6 个样本。

| 负载 | 原始 file / both 耗时比 | 按相邻原生归一化后的比值 |
| --- | ---: | ---: |
| getpid | 1.687 | 1.462 |
| 串行 fork/exec | 3.428 | 1.137 |
| 身份系统调用 | 0.622 | 0.648 |
| 文件描述符操作 | 2.060 | 0.815 |
| 映射/触页/保护/释放 | 1.127 | 0.752 |
| 四线程系统调用 | 1.221 | 0.771 |
| 四线程 fork/exec | 2.086 | 0.795 |

比值大于 1 表示本轮 both 耗时较少，小于 1 表示较多。第二轮没有证明全面、稳定的提速，不能只挑第一轮较好的数字。

`host-pressure-analysis.json` 从保存的 cgroup 计数计算负载。外层 CPU 配额为 36 核，本实验固定 4 核；各阶段外层整体平均消耗约 27–36 核。第二次 file 阶段 358 个配额周期中有 337 个被限流，随后原生阶段 102 个周期中有 99 个被限流。其他负载确实参与了竞争，前后归一化也不能消除非线性的调度和 I/O 干扰。因此两个开关保持显式实验选项，性能验收仍失败；不能承诺上述百分比会复现。

`host-memfd-flags.json` 还在实际 Linux 5.15.0-177-generic 上确认：带 MFD_EXEC 的创建返回 EINVAL，去掉该标志并保留 CLOEXEC/ALLOW_SEALING 则成功，支持代码中旧内核回退的具体判断。

## GPU 与持久 Docker 回归

`memfd-gpu-fd-v1` 的原生前置／关闭 GPU／启用 guest／原生后置四阶段通过；启用阶段完成全部 24 项控制关联与内存权限检查。两个内存后端开关在两个 guest 阶段均确认生效。`memfd-gpu-docker-v1` 的 12 项检查通过，包括配置保存、实际后端确认、设备权限、双容器并发、容器重启和整个实例重启；两次实例正常停止，无强制清理。内部 CUDA 仍未实现，这些结果不计为 GPU 验收通过。

`tests/docker-init` 在失败时输出完整 daemon 日志，保留之前被 warning/error 过滤器丢弃的 goroutine 堆栈。`create-diagnostic-disk.py` 从原私有测试磁盘复制新磁盘，仅替换 init，核对内容、执行权限、fsck 和原磁盘未变；没有修改原实例。命令套件和 Docker 二进制保持不变。该磁盘用于后续广覆盖回归。

## 广覆盖回归及收尾

`memfd-docker-suite-v1` 已结束：56 项中 53 项通过、内部 GPU 计算失败、2 项未测（该套件的外部网络与完整兼容性）。构建、资源限制、线程/映射、镜像操作、网络子集和本轮 Swarm 六项均通过。套件报告仍为失败，不能把 53/56 当作整个 Docker 的功能完成率。运行耗时 461.86 秒，guest 正常退出，没有强制清理；本轮超时上限是 600 秒，之前失败运行的上限是 360 秒，且启动磁盘 init 改了日志输出。因此本轮成功结束不证明 memfd 解决了历史 Swarm 超时，也不构成启动或执行提速证据。

`memfd-gpu-control-v1` 额外回归旧控制路径：原生前置、禁用 bridge、启用 guest、原生后置四阶段通过。启用阶段 13 项检查包括跨页/非法用户地址、错误恢复、dup、四线程及独立进程生命周期；实际 NVIDIA ioctl 153 次，来自 5 个 guest GPU worker。所有阶段正常退出。它仍是控制路径测试，没有 CUDA 计算。

`memfd-cleanup-audit-v1.json` 只读复核本轮 26 个进程组与记录的 worker/实例进程，没有残留；内核及两个 Docker 输入磁盘哈希未变，当前内核源码与补丁清单一致。原 `/root/udock` 只读健康检查返回 server 29.8.2、运行容器 0、镜像 6。`evidence-validation.json` 的 96 项检查核对源码快照、压缩跟踪哈希、样本数量、退出状态和报告计数；这是证据完整性检查，不是产品验收。三项总目标仍分别为 incomplete/fail/fail。
