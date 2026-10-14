# CUDA 诊断识别 guest 内的 UVM 节点

映射候选可以只提供标准 `/dev/nvidia-uvm` 节点。此前诊断脚本只尝试 `/dev/harddind-nvidia-uvm`，会把缺少别名误当作缺少整个 UVM 设备。现在依次检查这两个 guest 路径，仅在明确的设备不存在错误后尝试下一项，保留每次命令、stderr、退出码与清理结果。超时、权限错误和其他失败仍中止诊断；不查找外层宿主节点。

在已验证的旧内核 `9192f196143242aab3dfacc2966d7bcc605db9cc0caadfe68b09a76cb42b557e` 上实测：

- 两个路径均由内部 Docker daemon 报告设备不存在，CLI 实际均退出 127；`uvm_node_available=false`、`uvm_device_source=null`。
- 内部真实 CUDA 仍在打开 `/dev/nvidia-uvm` 时得到 `ENOENT`，计算未开始，运行器退出 1。没有把探测流程完成记作计算通过。
- 原生前后测均完成四卡计算、传输与每卡 1048576 个元素校验。
- 输入与依赖库未变、异步等待证据及正常关机检查通过；报告记录的 7 个进程组均为空。
- 39 项仓库测试通过；31 项原始日志、trace 和源码身份核对通过。

本轮验证的是“两种 guest 节点都缺失”的真实行为；标准节点可用时的计算路径仍须在修正后的映射候选中实测。完整 GPU、`--gpus all`、多进程与显存生命周期、近原生性能均未完成。当前带 ioctl/strace 追踪的数据不用于性能结论。

完整命令见 `cuda-uvm-discovery-v1-runs/status.json`。原始 stdout/stderr、syscall trace、运行器源码快照、输入哈希与实例状态保存在 `cuda-uvm-discovery-v1/`；`evidence-integrity.json` 和 `cleanup-audit.json` 分别记录证据核对和清理范围。
