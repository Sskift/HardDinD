# RM_CONTROL 越界指针错误语义修复

扩展根查询探针发现：原内核提前用 `access_ok` 拒绝越界指针，返回 `ioctl=-1, errno=EFAULT`；真实 NVIDIA 550.120 驱动返回 `ioctl=0, RM status=30`。`gpu-root-pointer-v1` 完整保留这轮失败：粗粒度的 22 个“成功／拒绝”检查虽然通过，但原始返回值有两项不同，guest 阶段明确为 fail。

新增可复现补丁 `0020-um-gpu-control-pointer-errors.patch` 保留 guest 地址范围检查。对于范围之外的非空地址，向真实驱动传入不可访问的非空宿主地址，避免把 UML stub 或 worker 私有映射暴露给 guest；返回时恢复原始指针。驱动自己决定返回值与错误顺序，桥不合成 RM status。

不能用 NULL 替代：原生空指针返回 `status=31`，不可访问的非空指针返回 `status=30`。当客户端也无效时，原生先返回客户端错误 `status=35`；新探针额外覆盖这个组合，验证错误优先级未被桥改变。

| 检查 | 结果 |
| --- | --- |
| 旧内核，`gpu-root-pointer-v1` | 原生前后通过；guest 的 wrap/noncanonical 两项原始返回不一致，阶段失败 |
| 新内核，`gpu-root-pointer-v2` | 原生前后与 guest 均 23/23；每次 104 个真实设备 ioctl；14 组返回值与 4 张卡信息一致 |
| `gpu-root-pointer-docker-v2` | 五次探针各 23/23；12 项外层权限、并发、重启和正常退出检查全部通过 |
| 清理审计 | 12 个进程组没有存活或僵尸进程；源码与补丁清单一致 |

新内核 SHA256 为 `06203d8fb96801f3843147185270157eb2d5311eb91588c75740f30384c17966`。本次构建与 stub 重定位检查通过，源码、旧失败与新结果、逐命令日志、压缩 syscall trace 和清理证据均保留。

运行方法沿用 [根查询实测](../starpub-2026-10-14-gpu-root-control/README.md) 的 `--rm-control` 与 `--native-baseline`，使用本次内核及新生成的探针。没有覆盖旧结果。

本轮只修复四种固定根查询的指针错误语义，仍没有设备 mmap、UVM、内部 CUDA、计算／传输或显存生命周期验收。并发 remap、更多 ioctl 与可取消等待仍待实现和验证；旧 RM_ALLOC 未开放类别的错误差异也未消失。没有进行性能对照，完整 GPU 与性能验收仍失败。
