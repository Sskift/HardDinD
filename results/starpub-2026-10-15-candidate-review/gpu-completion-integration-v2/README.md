# GPU 完成交接修复：完整补丁复现与接口回归

从固定 Linux 7.2.10 归档提取原始文件，使用主线实际 `apply-kernel-patches.py --gpu-mapping-experiment` 重新应用完整补丁序列。全部 33 个文件与 [实际 CUDA 测试内核的构建输入](../gpu-completion-v1/README.md) 逐字节相同；独立的 `--gpu-async-experiment` 序列也可应用，实际构建的 stub 对象通过重定位检查。

固定修复内核 `b8a58b85…`、异步等待开启、memfd RAM/stub 开启，完成原生前对照、禁用桥接负对照、4 vCPU guest、原生后对照。控制契约 **31/31**、分配契约 **74/74** 与原生一致，所有阶段正常退出，13 个记录进程组为空，源码和内核哈希未变。这是独立 guest 中的接口回归；Docker 内的四卡 CUDA 运行见前述实际计算记录，不把两种测试混为同一场景。

第一次补丁复现记录位于 `../gpu-completion-integration-v1/`：完整 mapping 序列已匹配，但 async-only 的准备目录漏放原始上游文件，应用器正确拒绝覆盖，整体退出码 1。当时未运行接口回归。v2 补齐原始文件后通过，保留首次错误及原始日志，没有覆盖或计作通过。

运行 `python3 recheck.py` 验证冻结哈希、33 文件绑定、全部实际接口记录和清理状态。无完整 GPU 或性能验收结论。
