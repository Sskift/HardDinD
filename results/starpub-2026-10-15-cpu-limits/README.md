# Docker CPU 限制的实际执行验证

在 Starpub 现有无特权容器中，通过上游 Docker CLI 操作内部 Moby daemon。只读外层 cgroup；没有增加宿主权限或使用宿主 Docker socket。这里检验 CPU 限制是否生效，不是接近原生性能的验收。

## v1：确认 cpuset 被丢弃，整体失败

运行时间为 2026-10-15 北京时间 03:31–03:38，其中包含等待构建锁的时间。使用 4 vCPU guest，内核 SHA256 `9192f196143242aab3dfacc2966d7bcc605db9cc0caadfe68b09a76cb42b557e`，测试工具对应 `f38370c`。原始结果见 [report.json](v1/runtime/report.json)。

| 检查 | 原始判定 | 实际观察 |
| --- | --- | --- |
| 不限额，4 个忙线程 | pass | 线程总 CPU 时间约 3.97 核，cgroup 约 3.98 核，没有节流 |
| `--cpus 1` | pass | cgroup 约 1.014 核，60 次节流 |
| `--cpu-period=50000 --cpu-quota=25000` | indeterminate | cgroup 约 0.493 核；原判定器把工作线程约 0.477 核判为低于供给边界，未计入采样线程消耗 |
| `--cpuset-cpus=0` | fail | Docker 明确警告 cpuset 被丢弃；工作线程仍分布在 CPU 0–3 |
| `--cpuset-cpus=1-2` | fail | 同样被丢弃，工作线程仍分布在 CPU 0–3 |
| cpuset 0 与 `--cpus 0.5` 组合 | fail | quota 有节流，cpuset 被丢弃 |
| 同一容器 `docker update`：2 → 0.5 → 2 | pass / indeterminate / pass | cpu.max 与容器身份均有实际观察；中间窗口的 cgroup 约 0.502 核，探针进程约 0.473 核，原判定将观察开销当成供给不足 |
| CPU shares 竞争 | not_tested | 后续独立实验，不能计为通过 |

三个 cpuset 启动命令的 stderr 都包含 `Cpuset discarded`，`docker inspect` 中 cpuset 为空，容器内 `cpuset.cpus.effective` 返回 ENOENT。对应的 [实际内核配置](v1/baseline-kernel.config) 是 `# CONFIG_CPUSETS is not set`。这证明此前配置写入或命令退出码成功不足以证明限制功能可用。

该轮实例正常关机，没有强制关机。69 个命令进程组加 guest、网络辅助进程，共 71 个记录的进程组在独立检查时为空。[归档复核](v1/archive-audit.json) 用该轮冻结的原判定器重算固定阶段和动态更新阶段，结果与原报告完全一致；没有用后来的判定器覆盖旧结果。输入哈希、原始 stdout/stderr、运行器、探针、判定器和内核配置均已保存。

## 修复与待验证范围

内核配置补上 `CONFIG_CPUSETS=y`，新内核正在重建，仍需新的真实 Docker 结果证明修复。

判定器的配额供给下限改为检查整个 cgroup 的 CPU 用量，因为配额也包含采样线程和 `docker exec` 观察进程的消耗。工作线程、进程与 cgroup 仍须满足原有计数一致性检查，所有计数仍须低于相同配额上限，同时要求实际节流、足够的不限额负载以及正确的 cpu.max。新增采样开销正例和无法解释的大额非工作线程消耗负例；33 个判定器测试在 Starpub 通过。该修正没有调整配额边界，也没有引入性能验收阈值。

GPU、多卡与显存生命周期、其他 Docker 命令、CPU shares 和接近原生性能仍分别需要完成。
