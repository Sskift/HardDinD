# GPU 生命周期 v5：四卡原生对照通过

Starpub 的 4 × A100-PCIE-40GB、NVIDIA 550.120 上，实际原生运行退出 0，独立解析器判定通过。内部 Docker 未运行此生命周期探针；内部 CUDA 初始化仍失败，因此本结果不代表内部 GPU 或性能验收通过。

本轮修复 v4 实际失败：保持 context 为 current 时 detach，再清除 current；顶层参数解析接受子进程 ready/release FD；管道关闭通过 EPIPE 返回并完成回收，避免 SIGPIPE 杀死父进程。原有真实 JSON 布尔值和严格子进程协议修复保留。

实际运行有 23 条记录、16 个 worker、136 次计算/传输/校验循环；每次包含三个 GPU 缓冲区分配与释放。覆盖四卡逐卡重复 context 创建销毁、四线程共享 context、四卡各一线程并发、四个 fork/exec 子进程。观测到共享线程、多卡线程、子进程计算区间分别有 38.52 ms、26.36 ms、7.59 ms 重叠。这些数值证明本轮重叠，不能用作吞吐或性能达标阈值。

独立 owner 从冻结源码重新编译，执行 ASan/UBSan 主机 fixture、真实子进程参数/管道检查和 66 项 Python 检查，无失败或跳过。GPU 清单前后一致，源码和动态库哈希稳定，11 个记录的命令进程组均正常退出，没有强制回收。原始 `native.stdout.gz`、`native.stderr.gz`、`native-verdict.json`、命令日志和清理记录均保留。

源码与探针合入主仓库。`scripts/run-gpu-cuda.py --probe lifecycle` 可执行原生前/内部 Docker/原生后对照；合入时保留主线已修复的 canonical device 选择、构建锁和每个命令的进程/清理记录。归档中的作者脚本是冻结输入，主线集成脚本另行验证，不能把原生探针通过说成已运行整个内部对照。

```sh
python3 results/starpub-2026-10-15-candidate-review/recheck-runtime-candidates.py
```

该命令离线核对归档与源码哈希、重解析真实 CUDA 记录，并检查清理与验收边界。后续仍需内部 GPU 计算、`docker --gpus` 集成、UVM、显存压力、IPC、异步复制和同机性能测试；全局空闲显存计数不能证明共享设备上的物理内存无泄漏。
