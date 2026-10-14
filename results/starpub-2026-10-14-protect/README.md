# 内存权限同步实验与回归

本轮保留原版 Docker/containerd/runc，修改 UML Linux 内核。新补丁 `0010` 只在已知宿主映射存在、物理页未改变时用 `mprotect` 同步权限，合并相邻且权限相同的范围；新映射、清除 PTE 和更换物理页仍使用原路径。撤销权限和映射的同步要求没有取消。实验默认关闭，通过 `mprotect_sync=1` 启用，尚未达到完整 Docker、原生性能和 GPU 目标。

最终内核 `protect-kernel-v3` SHA256：`dee5acab165c0fcd0b354ea4db0698410066e3b83f4c01d643b4f81f3fa91b8e`。它包含 `0001`、`0003` 至 `0010`；基线为上一轮包含 IPI 空闲修正的 `ipi-kernel-idle`。所有本轮性能比较保持 `defer_ipi=1`、4 个 vCPU、宿主 CPU 0–3、spin 256。CPU 没有独占。

## 已定位和修补的原因

原路径对需要同步的 present PTE 重新 mmap，即使只改读写权限。通用的权限变更事务还会先清空 PTE，使简单比较丢失原映射身份。补丁增加内部提示位：只有原 PTE 已同步或已被证明只改权限，并且新旧 PFN 相同，才登记权限更新；不能从另一个 PTE 或地址空间继承该提示。UML 没有硬件直接更新 PTE 的 A/D 位，因此实验路径在持有 PTE 锁的权限事务中保留旧 PTE，提交时登记变化；关闭实验保留原清除行为。同步器仍用 cmpxchg 清除同步标记，不能吞掉并发更新。

宿主调用追踪为同一 v3 内核的开关对照，完整原文保存在两个 `protect-v3-trace-*.strace.gz` 中，分析见 `protect-v3-trace-comparison.json`。扣除被 seccomp 截获而未在宿主执行的 guest 调用后：

| 场景，均为 8 轮 | 关闭权限优化 | 开启权限优化 |
| --- | --- | --- |
| 已有 16 页的两次权限切换 | 176 mmap、0 mprotect | 0 mmap、16 mprotect |
| 新建 16 页、触页、改权限 | 226 mmap、0 mprotect | 128 mmap、8 mprotect |
| 仅新建并首次触碰 16 页 | 128 mmap、128 SIGSEGV | 128 mmap、128 SIGSEGV |

这证明权限更新的多余映射被移除，同时也显示首次触页的逐页成本仍在。计数含阶段内的辅助工作；不能用被追踪的耗时估算提速或成本比例。本地重算分析与远端逐字节一致。

## 早期启动失败及编译修正

v1、v2 都启动失败，保留日志、JSON、v2 追踪、目标文件符号与重定位记录，以及 `fixtures/0010-v1.patch`、`0010-v2.patch`。新增 switch 分支使 GCC 生成了位于内核 `.rodata` 的跳转表，而 syscall stub 会被搬到独立用户地址空间，那里没有对应的内核 `.rodata`。v2 追踪记录 broker 在 `0x605fa030` 发生 SIGSEGV；目标文件存在指向 `.rodata` 的重定位，随后映射同步超时。这是已查明的编译产物问题，不是把失败当成测量噪声。

v3 对 stub 禁用 jump table 和 switch table 转换，并新增 `check-stub-relocations.py` 检查搬移代码只能引用同节内符号。实际 v2 对象检查失败，v3 通过；构建脚本在链接后执行该检查。v2 还补了 `um_mprotect_sync` 的模块符号导出，最终版本保留。v3 的后续所有本轮测试正常结束；没有因此关闭此前独立的偶发宿主 child 丢失问题。

## 正确性与 Docker

新增静态程序 `src/mapping_semantics.c` 检查真实读写效果和信号，包含三组：

- PROT_NONE、只读写入触发 SIGSEGV、恢复权限、MADV_DONTNEED 后零填充。
- 同一虚拟地址交替映射两份不同内容的文件，检查正确文件、私有写入和共享别名不被污染。
- fork 后私有写入不影响父进程；文件截断后触发 SIGBUS，重新扩展后读取零页。

同一程序在外层原生、上一版内核，以及 v3 的 spin 0、256 下均通过。三个 guest 运行同时通过已有 5 组线程语义和 36 项微基准测量。零轮询与 256 轮询的实际权限同步页数分别为 41,626 和 41,657；程序哈希在运行报告与 provenance 中一致。此覆盖不等于全部多线程页表竞态或安全隔离验证。

`docker-protect-v3.json` 是真实 Docker 的完整当前测试脚本：**56 项返回，53 pass、1 GPU fail、2 not_tested**，314.28 秒结束并正常关机；新增容器内映射语义项目通过。实际权限同步页数为 323,580。GPU 后端仍未实现，出站网络和完整兼容性仍未测。此前 Swarm 退出偶发超时与强制清理记录保留，不能因这轮通过就称已修复。范围见 [命令覆盖说明](../../docs/command-coverage.md)。

## 性能：专项改善与综合回退同时保留

专项未加追踪，顺序为 native/off/on/on/off/native，每模式每项 6 个样本。`protect-profile-comparison.json` 用每轮全部耗时除以迭代数得到下表中位数，单位 µs；不是完整产品验收。

| 每次迭代工作 | 原生 | 关闭优化 | 开启优化 |
| --- | ---: | ---: | ---: |
| getpid | 0.612 | 4.511 | 4.246 |
| 新建并触碰 16 页 | 38.712 | 331.641 | 331.150 |
| 新建、触页并修改权限 | 40.536 | 523.651 | 326.111 |
| 已有映射两次权限切换 | 4.746 | 95.306 | 72.212 |
| fork + wait | 99.832 | 1083.843 | 997.398 |
| fork + exec + wait | 255.079 | 2331.062 | 2031.360 |

目标权限切换中位数下降约 24.2%，新建并改权限下降约 37.7%；首次触页基本不变。小样本和共享 CPU 下的其他波动不能全部归因于补丁。

第一次 8 组综合对照 `protect-v3-comparison.json` 全部完成，但第二个 on 运行严重变慢：getpid 的三个样本为 1.937、11.769、8.947 秒，整个运行耗时 54.02 秒，第一个 on 为 14.35 秒。第一轮没有逐模式记录宿主负载/限流，不能认定原因是邻居负载，也不能删除慢样本后宣布成功。

因此追加 `protect-v3-recheck-comparison.json`，保存逐模式的 cgroup CPU 使用/限流、压力和 loadavg。追加结果也全部完成，下面是 6 样本中位数，单位 ms：

| 测试 | 原生 | 上一版内核 | v3 关闭 | v3 开启 |
| --- | ---: | ---: | ---: | ---: |
| 10 万次 getpid | 51.080 | 396.738 | 418.514 | 398.805 |
| 100 次 fork/exec | 28.667 | 170.447 | 218.137 | 189.966 |
| 映射/触页/权限/释放 | 19.726 | 179.778 | 203.170 | 162.051 |
| 四线程系统调用 | 24.329 | 204.043 | 218.452 | 295.402 |
| 四线程并行 fork/exec | 13.812 | 243.713 | 251.191 | 272.737 |
| 四线程纯计算 | 384.567 | 378.040 | 383.033 | 389.674 |

同内核映射耗时下降约 20.2%，但四线程系统调用增加约 35.2%，并行 fork/exec 增加约 8.6%。新版本开启后串行 fork/exec 也未优于上一版内核。本轮第二个 on 期间外层 cgroup 有两次限流、累计约 0.627 秒，但只有整轮聚合数据，不能用它解释某一测试或证明首轮慢样本的原因。

所以**保持实验开关默认关闭**，不能称为全面提速。追加对照中系统调用、串行 fork/exec、映射仍分别约为原生 7.81、6.63、8.21 倍。下一步须定位多线程同步/交接和不稳定慢样本，并推进外部网络、持续命令入口与 GPU，不能仅继续挑选有利微基准。

## 复现与证据完整性

```sh
make kernel-protect-experiment WORK=/root/harddind-rebuild
# 保存不可变内核副本后再做对照；以下 NAME 必须是未使用名称。
make mapping-root WORK=/root/harddind-rebuild
python3 scripts/run-core.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --threads-experiment --broker-cache \
  --defer-ipi --mprotect-sync --mapping-semantics --dispatch-bench \
  --disk /root/harddind-rebuild/mapping.ext4 --spin 0 --result-name NAME
python3 scripts/compare-ipi.py /root/harddind-rebuild \
  --baseline /path/to/ipi-idle-kernel --kernel /path/to/immutable/kernel \
  --feature mprotect-sync --name NAME
python3 scripts/profile-dispatch.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --mode guest --defer-ipi --mprotect-sync --name NAME
```

`mapping-root` 依赖干净的 `dispatch-root`。Docker fixture 必须重建以包含新程序和测试脚本；本轮使用独立 `protect-docker.ext4`，所有运行均使用独立 COW 层。性能测量串行，未与编译、磁盘哈希或其他本项目测试并行。

本机和 Starpub 的 `make check` 均通过 12 项检查及脚本语法检查。临时源码副本完成 `0010 → 0009 → 0010` 往返，并确认回退源码等于上一轮 provenance、最终源码等于实际构建 stamp。`protect-v3-provenance.json` 包含内核、配置、磁盘、程序、补丁和运行器哈希；`protect-v3-relocation-check.json` 是最终 stub 检查。验证辅助脚本保存在 `fixtures/`。

最终检查的 26 个实验进程组均无残留，也没有遗留 COW 文件。原有 `/root/udock` 只读健康检查为 Docker 29.8.2、运行中容器 0、镜像 6；没有安装或替换原服务。证据见 `protect-final-checks.json`。总验收继续为功能 incomplete、性能 fail、GPU fail；不因本轮局部修补变更目标或性能阈值。
