# 固定参数对象分配：CUDA 越过 0xcb33

实验补丁 `0026-um-gpu-fixed-alloc.patch` 支持 NVIDIA 550.120 的 `NV_CONFIDENTIAL_COMPUTE (0xcb33)` 分配。实际内部 Docker 中的 CUDA 已完成该分配，随后在 `RM_CONTROL 0x1f0` 返回 EOPNOTSUPP，`cuInit` 报错 999；**内部计算仍未执行，完整 GPU、Docker 功能及近原生性能仍未完成。** 这里使用的是对象类的名字，不代表实现了机密计算功能。

固定驱动的 `class/clcb33.h` 定义可选的 4 字节 `hClient`，`rs_access.h` 定义 4 字节权限掩码。非 FINN 分配在 `rmapi/alloc_free.c`、`param_copy.c`、`resource_desc.c` 中按对象类推导长度，不能因为 CUDA 传入 `paramsSize=0` 就拒绝或少复制数据。补丁分别处理 NVOS21 与 NVOS64 的不同尾部布局，同步参数页权限，转发真实句柄和原始标志；越界非空指针改为宿主无效地址，并在头部回写前恢复原指针。真实驱动决定状态码及错误顺序。

| Starpub 同机实测 | 结果 |
| --- | --- |
| 原生前后分配探针 | 各 13 项功能检查通过；57 条原始结果前后一致 |
| guest 分配探针 | 13 项功能检查通过；56/57 条与原生一致，总结果失败 |
| 五次实际 Docker 运行 | 首次、容器重启、两个并发客户端、实例重启后，均为 56/57 条一致，总结果失败 |
| 分配生命周期 | 每次探针含四线程、四个独立子进程，各执行 50 轮分配和释放 |
| 权限和清理 | 无设备、非 root、只读设备、配置不匹配、输入哈希、正常关机检查通过；25 个异步 worker 已核对 |
| 策略回归 | 保持 105/107 条与原生一致；原有未知命令和继承 FD 两项差异仍失败 |
| 实际内部 CUDA | `0xcb33` 分配 rc=0、RM status=0；下一条 `0x1f0` 返回 errno 95，内部计算失败 |
| 实际原生 CUDA 前后测 | 各完成四卡计算、上传、下载和每卡 1048576 个元素校验 |
| 构建与证据 | 14 个固定头文件哈希及 ABI 编译检查通过；34 项仓库测试通过；389 项取回证据核对通过 |

唯一的分配结果差异是 `v64_flags_finn`：本补丁尚不转发 FINN 序列化分配，返回 EOPNOTSUPP；原生此次返回 ioctl 成功、RM status=106。该差异保留为失败，没有放入通过白名单。57 组对照包含自动/固定句柄、空/错误客户端、32/48 字节头部、非规范声明长度、空/低地址/非规范/溢出指针、只读/不可访问/跨页参数、只读头部以及权限掩码。它们不是整个 NVIDIA 分配 ABI 的覆盖声明。

本轮内核 SHA-256：`435d4d8dbe7ccc249722984fa7434f1f55085cc96c82988c0f8d0659e3511694`。仅进入显式 GPU 实验构建，本轮运行开启异步等待。原始报告、源码快照、构建配置、压缩 stdout/stderr/strace 均保存在同名子目录。`evidence-integrity.json` 重读原始记录并核对报告、调用结果和哈希；`cleanup-audit.json` 确认 18 个记录的进程组均为空，本轮构建及实验进程已退出。追踪影响耗时，本轮不提供性能结论。

复现时使用新的实例名和结果名：

```sh
make kernel-gpu-async-experiment WORK=/root/harddind-rebuild
cp /root/harddind-rebuild/thread-build/linux /root/harddind-rebuild/gpu-alloc-repro-linux
bash scripts/check-gpu-alloc-abi.sh
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-alloc-repro-linux \
  --name gpu-alloc-repro --alloc-object --async-wait
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-alloc-repro-linux \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --probe /root/harddind-rebuild/results/gpu-alloc-repro/gpu-guest-probe \
  --native-baseline /root/harddind-rebuild/results/gpu-alloc-repro/report.json \
  --name gpu-alloc-docker-repro --alloc-object --async-wait
python3 scripts/run-gpu-cuda.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-alloc-repro-linux \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --name cuda-alloc-repro --async-wait
```

三个运行器当前都退出 1。CUDA 报告的 `diagnostic_complete=true` 只表示诊断流程完整；验收字段 `internal_cuda_verified=false` 仍表示内部计算失败。分配探针退出 0 表示基本生命周期检查通过，运行器仍会因原生对照差异退出 1。
