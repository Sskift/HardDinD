# NUMA 查询修复的实际结果：CUDA 初始化仍失败

Starpub 无特权容器内，owner 从固定源码独立强制重编译 14 个修改文件，前后 33 个关联源码哈希一致；补丁重新应用到固定基线后逐文件相同。候选内核 SHA256：`601c0b181dc1a743b1b50b17bab50fdf65842fe487e665eb170c98f72950ebd9`。

本轮修复 `0xc23046d7` 的 NUMA 查询：实际 ioctl 编号为 215，结构体需要 8 字节对齐，offline 数组位于偏移 40，整体为 560 字节。作者初稿的编号 15、packed 布局和声明顺序错误在实际运行前已修正。

| 实际运行 | 结果 |
| --- | --- |
| 系统能力：原生前、关闭后端、guest、原生后 | 31/31 合同与原生匹配 |
| 分配边界：同样四个阶段 | 74/74 合同与原生匹配 |
| 原生 CUDA 前后对照 | 每次四卡均执行计算、传输及结果校验 |
| 内部 Docker NUMA 查询 | 四张卡的查询均返回成功 |
| 内部 Docker CUDA | `cuInit(0)` 返回 100，无内部计算结果 |

实际剩余 ioctl 失败只有两类，各出现四次：

- `RM_CONTROL 0x20810108`，参数 992 字节，返回 `EOPNOTSUPP`。原生调用成功；尚未获得可证明该结构布局及指针语义的固定源码依据，不能仅根据大小加入白名单。
- `NV_ESC_ALLOC_OS_EVENT 0xc01046ce` 返回 `EINVAL`。真实 CUDA 把 NVIDIA 字符设备的 fd 放入该字段；候选错误地要求它是 eventfd。固定驱动 `escape.c` → `rm_alloc_os_event` → `allocate_os_event` 把该值作为绑定 nvfp、以 hParent/fd 索引的事件标识。现有 eventfd 模型不足以代表这个实际协议，正在修正完整事件分配、释放、通知及错误通道。

候选同时加入 `GPU_DETACH_IDS 0x216` 的固定 128 字节布局，但本轮 trace 中没有执行该调用，因此它未获实测通过。`abi-reference/` 保留 NUMA 定义和 detach 的实际实现；后者对 attached ID 做检查，不能仅依据头文件注释声称完成了完整 detach 生命周期。

guest 正常关机，源码、二进制及驱动库哈希稳定，25 个记录进程组经独立复核均为空。`diagnostic_complete=true` 仅表示本轮诊断流程完整；`internal_cuda_verified=false`，整体功能及性能验收均未通过。补丁仍是候选，未替换正式后端。

在仓库根目录执行 `python3 results/starpub-2026-10-15-candidate-review/gpu-mapping-v10/recheck.py`，可重新应用补丁、核对源码绑定、从原始输出重算控制接口结果、验证剩余错误和清理记录。
