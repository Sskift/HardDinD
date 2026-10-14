# CUDA 驱动版本查询的二级指针桥接

补丁 `0022-um-gpu-build-version.patch` 接入 NVIDIA 550.120 的旧版 `RM_CONTROL 0x101`。真实内部 Docker CUDA 程序已通过这一查询，但在下一条 `0xd04` 返回 `EOPNOTSUPP`，`cuInit` 返回 CUDA 错误 304。内部计算、传输与结果校验仍未通过；完整 GPU 和性能目标保持未完成。

原生驱动先处理客户端，再读取固定 40 字节的子结构；子结构还包含三个字符串地址。桥接固定两层参数，向真实宿主驱动传递经过边界检查的输出地址，完成后恢复原始指针。没有伪造驱动版本或返回状态。共享参数区由 64 扩为 128 字节，缩减 syscall 数组保留空间，静态断言仍要求整个 stub 数据区为两页。

原生实测与固定源码共同确认了几个容易误实现的行为：声明的子结构大小为 39 或 41 仍成功；任一字符串地址为空就只查询所需长度；子结构只读时，字符串仍可写入且调用成功，结构回写失败被驱动忽略；字符串写入失败会保留此前已完成的写入；外层参数头只读则返回 `EFAULT`，此前副作用仍存在。探针逐字节比较三个 256 字节缓冲区，而非只判断成功或失败。

| 证据 | 实际结果 |
| --- | --- |
| `gpu-build-native-design-v1` | 首次原生探索中，两项关于声明大小的预期错误；原始失败日志保留，依据真实行为修正探针 |
| `gpu-build-version-kernel-v1` | 首次构建暴露旧的 64 字节布局断言；修正后构建和 stub 重定位检查通过，两次日志均保留 |
| `gpu-build-version-v1` | 原生前后与 guest 各 30/30；24 组原始状态、指针保留情况和缓冲区内容一致；默认关闭阶段通过 |
| `gpu-build-docker-v1` | 五次内部 Docker 探针各 30/30，与原生数据一致；权限、并发、容器与实例重启等 11 项检查通过 |
| `cuda-build-version-v1` | 原生前后四卡计算通过；内部实际 `0x101` 成功，随后 `0xd04` 失败；内部计算未执行 |
| `gpu-build-root-regression-v1` | 原有根查询探针原生前后和 guest 各 23/23，原始状态一致 |
| `gpu-build-core-v1` | 四 CPU 线程语义与 18 个微基准样本完成；这里只作功能回归，没有原生性能对照 |

内核 SHA256：`00d0cb7630dbd2e2feb5eb477a9c9ed140d040ac854c7189cc10b061e0953f59`。另运行现有 20 项 Python 单元测试，全部通过。19 个实验进程组结束后无存活或僵尸进程，源码和补丁清单一致；完整性核对见 `evidence-validation.json`。

原生每次探针产生 71 个设备 ioctl，guest 为 70 个：不可读的外层参数头在 guest 复制阶段即返回 `EFAULT`，不会提交给宿主。每次 guest 探针有五个独立 GPU worker，覆盖四线程共享客户端和四个独立子进程。

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
# 将 thread-build/linux 复制为独立不可变内核后运行：
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name build-query-new --build-version
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --probe /root/harddind-rebuild/results/build-query-new/gpu-guest-probe \
  --native-baseline /root/harddind-rebuild/results/build-query-new/report.json \
  --name build-query-docker-new --build-version
```

这一补丁仍是限定 ABI 的桥接，只接入该命令的零 flags 路径。输出地址声明范围跨越 guest 用户空间边界时采用保守拒绝；并发重映射等边界尚未验证。更多对象、设备 mmap、UVM、显存生命周期、可调度等待及 `--gpus all` 尚未完成。下一条 `0xd04` 是 `NV0000_CTRL_CMD_CLIENT_SET_INHERITED_SHARE_POLICY`，参数为 12 字节客户端继承共享策略，需要按真实驱动语义继续接入。

标准输出、错误输出与 trace 以可解压的 `.gz` 保存；没有收录磁盘、可执行文件、驱动库或 CUDA 缓存。原始失败不计为通过，功能诊断完成也不计为完整 CUDA 或接近原生性能验收。
