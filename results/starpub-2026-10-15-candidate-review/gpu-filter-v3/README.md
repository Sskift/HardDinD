# GPU 过滤器修复的独立验证

2026-10-15（北京时间）在 Starpub 重建并执行了候选内核实际 BPF 数组的两种模型。`gpu_bridge` 与 `gpu_async` 的四种组合各执行解释器和真实 seccomp 模型一次，**8 次均退出 0**，没有超时或强制清理；结束后独立检查的 8 个进程组均为空。

解释器每组检查 24 个系统调用判定，共 96 个；真实 seccomp 模型每组执行 5 个代表调用，共 20 个，包括 getpid 的 TRAP、三个 write fd 和 close。真实模型没有执行 clone 或 rt_sigreturn，不能把解释器覆盖写成这些调用的真实内核验证。过滤器正文直接从冻结的 `stub_exe.c` 提取，SHA256 为 `57a217f1ff95d26430886f520b07b2774be0f73b8f41d729ede30d3b58e42b7c`。

`snapshot/` 保留候选原始源码。原始真实模型的 fork 失败路径没有检查负 PID，且子进程自行改变进程组，原生成器的超时不保证回收所有子进程。**不要直接运行该历史模型或生成器。** `validation/real-model.c` 是主代理执行前修正的版本：检查 fork、sigaction 和 prctl，子进程留在外部 supervisor 的进程组，只对已验证的正 PID 执行 watchdog kill，并明确把 write 的长度寄存器设为 1。过滤器正文没有修改。`validation/supervisor.py` 保存本次远端运行编排，使用全局排他构建锁、严格编译参数、逐次超时和进程组回收；其中远端绝对路径与 cleanup 模块依赖按原样保留。

完整候选内核当时**构建失败**：新增 `.text` 尺寸检查存在平台兼容问题。冻结的 Makefile 使用 `awk strtonum`；保存的失败日志来自后续一次修复尝试，报错为 `/bin/sh` 不支持 `16#00000726` 算术语法。二者不是同一次构建输入，不能用该日志证明冻结 Makefile 的具体报错。失败日志和脚本保存在 `snapshot/failed-build.*`。过滤器模型通过只排除了这一组调用可达性回归，不能说明内核已经构建、启动或内部 CUDA 已经可用。完整映射补丁、实际内部容器计算及性能验收仍未完成。

`snapshot/manifest.json` 是远端完整快照的清单，仓库没有收录其中 12 个二进制和停止观察进程的记录；两个生成的 include 文件保存在 `validation/`。不能把这个清单描述为仓库内完整构建闭包。`validation/report.json` 固定模型源码、编译命令、二进制哈希、退出状态；`owner-audit.json` 保存编译器版本、原始快照清单哈希和独立清理检查。

本地仅重解析证据，不编译或执行模型：

```sh
python3 results/starpub-2026-10-15-candidate-review/gpu-filter-v3/recheck.py
```
