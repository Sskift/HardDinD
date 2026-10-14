# 系统能力查询与分配错误顺序

`0027-um-gpu-system-features.patch` 接入固定 4 字节的 `NV0000_CTRL_CMD_SYSTEM_GET_FEATURES (0x1f0)`，并将 `0xcb33` 的 FINN 分配错误判定交回 NVIDIA 550.120 驱动。**完整 GPU、Docker 功能和近原生性能仍未完成。**

两版实际 CUDA 均已通过 `0x1f0`，获得与原生相同的 `0x0b` 能力掩码。随后 `/dev/nvidia-uvm` 的两次打开均返回 `ENOENT`，`cuInit` 报错 999，内部计算未开始。原生前后测分别完成四卡计算、上传、下载及每卡 1048576 个元素校验。原始 syscall trace 与报告哈希一致，当前版本定位证据见 `cuda-system-features-v2/docker.strace.gz`。

`0xcb33` 的分配路径在固定源码 `serverAllocResource` 中先调用 `serverAllocApiCopyIn`，按对象类复制可选的 4 字节 `hClient` 和 4 字节权限掩码，再进行 FINN 解码。该驱动的 FINN 分配接口不支持此类；直接转发原始 flags 能保留驱动的状态、参数复制错误和判定顺序。此前桥接层提前返回 EOPNOTSUPP，造成了与原生的差异。修正只适用于已核实的固定结构，没有开放未知结构或原样转发未知指针。

| 已完成的同机实测 | 结果 |
| --- | --- |
| 扩展分配边界 | 原生前测、关闭桥接、guest、原生后测全部通过；74/74 条结果一致 |
| 分配 Docker 生命周期 | 首次、容器重启、两个并发客户端、实例重启后，五次均 74/74 条一致；权限及正常关机检查通过 |
| 首版系统能力边界 | 原生前后 31/31 一致；guest 和五次 Docker 运行均 17/31 一致，总结果失败 |
| 第二版系统能力边界 | 原生前测、关闭桥接、guest、原生后测全部通过；guest 与五次 Docker 运行均 31/31 条一致，Docker 外层 12 项检查通过 |
| 第二版分配回归 | 四个阶段全部通过；guest 与五次 Docker 运行均 74/74 条一致，Docker 外层 12 项检查通过 |
| 实际内部 CUDA | `0xcb33`、`0x1f0` 成功；UVM 设备缺失，内部计算失败 |
| ABI / 仓库检查 | 17 个固定头文件哈希及编译断言通过；39 项仓库测试通过 |
| 证据与清理 | 首版 77 项、第二版 87 项取回证据核对通过；两版各自记录的 22 个进程组均为空 |

首版系统能力的 14 条差异来自一个漏改的旧尺寸/flags 过滤条件，包含声明长度、FINN、未知 flags 以及无效客户端组合。**当前代码已经修正这个条件，第二版实测消除了这 14 条差异。** 首版失败记录保留。分配探针新增 17 条 FINN 与尺寸、指针、页保护、权限掩码的组合测试，没有将失败或缺测排除出对照。这些控制接口回归通过不代表内部 CUDA 计算通过。

内核 SHA-256：

- v1，保留首版失败实测：`601b6b5411eba6898ad6660701795cd3981dec83377c4d2e82389d3bf07f68ee`
- v2，当前代码，运行回归已完成：`9192f196143242aab3dfacc2966d7bcc605db9cc0caadfe68b09a76cb42b557e`

`gpu-system-features-kernel-v1` 保留首版补丁，v2 目录保留当前修正版补丁和构建记录。各探针运行器保存源码快照、原始 stdout/stderr、strace、状态及哈希；`evidence-integrity-v1.json`、`evidence-integrity-v2.json` 重新解析原始结果，并核对 trace、补丁、内核和报告身份。`cleanup-audit-v1.json`、`cleanup-audit-v2.json` 分别覆盖两版已结束的实验；并行开发任务不包含在清理声明中。第二版串行队列中，四项控制接口运行及最后的仓库检查均退出 0，CUDA 如实退出 1。当前测试带追踪，不用于性能结论。

复现入口：

```sh
make kernel-gpu-async-experiment WORK=/root/harddind-rebuild
cp /root/harddind-rebuild/thread-build/linux /root/harddind-rebuild/gpu-features-repro-linux
bash scripts/check-gpu-alloc-abi.sh
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-features-repro-linux \
  --name gpu-features-repro --system-features --async-wait
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-features-repro-linux \
  --name gpu-alloc-finn-repro --alloc-object --async-wait
```

实际 Docker 运行使用 `run-gpu-control-docker.py` 的同名选项，并传入对应探针二进制与原生前后基线报告；CUDA 使用 `run-gpu-cuda.py`。每次使用新的实验名，保留先前结果。运行器仍区分“观察流程完成”和“内部计算通过”，不能用前者替代 GPU 验收。
