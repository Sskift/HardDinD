# 真实 CUDA 的客户端共享策略参数

本轮没有扩大 GPU 命令覆盖。诊断 logger 为 NVIDIA 550.120 的 `NVOS54` 控制请求增加有界二级参数快照，以明确下一条失败命令 `0xd04` 的真实输入。最多读取声明长度和 64 字节中的较小值；通过 `process_vm_readv` 处理不可读地址，使用原始指针记录调用前后数据，恢复真实 ioctl 的 errno，不改写请求或返回值。追踪时序不能用于性能验收。

同一个计算程序按原生前测、内部 Docker、原生后测运行，使用已验证的异步内核 `ec7c521783615a20a070c319765aca81a2b4e610f43b80865d9ee932cd555f8f`：

| 阶段 | 结果 |
| --- | --- |
| 原生前测 | 四张 GPU 均完成上传、计算、下载和 1048576 个元素校验；2725 条 ioctl 记录 |
| 内部 Docker | 8 条 ioctl 后仍在 `0xd04` 返回 `EOPNOTSUPP`，CUDA 错误 304，未执行 GPU 计算 |
| 原生后测 | 四卡计算和结果校验通过；2725 条 ioctl 记录 |

两次原生运行均观察到相同的两条策略，参数调用前后不变：

| 顺序 | target | accessMask | type | action |
| --- | --- | --- | --- | --- |
| 1 | 当前 RM 客户端句柄 | 1 | 4 (`RS_SHARE_TYPE_PID`) | 0 |
| 2 | 当前 RM 客户端句柄 | 4 | 4 (`RS_SHARE_TYPE_PID`) | 4 (`COMPOSE`) |

内部 Docker 的第一条请求与此形状相同，尚未到第二条。`target` 实测是 RM 句柄，不能因类型叫 PID 就将其替换成宿主进程号。固定 NVIDIA 源码 `5e52edb2034de7db4d8ae368dbc7c26b416bfa16` 的 `src/common/sdk/nvidia/inc/rs_access.h:267` 定义该 12 字节结构；`src/nvidia/src/kernel/rmapi/client_resource.c:208` 的 PID 策略回调比较源、目标客户端记录的 `ProcID`。接入时仍须保留稳定的 worker 身份并验证跨客户端语义。

[policy-observation.json](policy-observation.json) 保存逐条参数、原始日志行和 23 项证据核对。真实异步 worker、输入和驱动库哈希、正常关机均确认；记录的五个实验进程组为空。原始报告与压缩日志保存在 `cuda-policy-trace-v1/`，完整 GPU 与性能验收仍未通过。

复现入口：

```sh
python3 scripts/run-gpu-cuda.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-async-kernel-v1 \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --name cuda-policy-trace-new --async-wait
```

当前内核的命令退出码应为 1，因为内部 CUDA 仍失败；`diagnostic_complete=true` 仅表示诊断流程完整。
