# Starpub 交接协议实验与重复回归

**结论：完整目标未达到；实验补丁未纳入默认构建。**

在同一个无特权 Starpub 容器中完成。外层宿主内核 5.15.0-177-generic，guest 为 Linux 7.2.10 UML。`handoff-build.json` 记录两个内核及补丁的 SHA256；所有 CPU 测试使用同一份静态程序。默认内核包含补丁 0001，实验内核增加 `kernel/experiments/0002-um-wake-only-sleeping-peer.patch`。

交接协议直接使用内核补丁中的 `stub-futex.h` 测试，在线程和独立进程模式各运行三档轮询值，每组 10 万次，共 60 万次。每次验证请求和回复序号；睡眠模式包含真实 EINTR。全部完成，没有超时或校验失败。见 `host-probes.json`。这只验证交接协议，不等于 guest 隔离或完整 Docker 验收。

分别实测 `unshare`、`clone`、`clone3` 的 24 种组合。普通 `clone` 成功；带 user/mount/network/PID/cgroup 标志的 `clone` 返回 EPERM，`unshare` 返回 EPERM，`clone3` 返回 ENOSYS。进程的 capability、seccomp 和 root 身份一同记录在 `host-probes.json`。

性能对照按 before/after/after/before 交错运行，每个轮询值下每项各有 6 个样本。保留了 12 次完整启动日志和数据。`handoff-comparison.json` 表明没有稳定收益：spin=256 时系统调用中位数增加约 7.3%，fork/exec 降低约 6.4%；spin=4096 时这两项都未改善。因此默认构建不采用这个实验，不能称为性能优化成功。

以下为 spin=256 的诊断微基准，单位为秒；原生每项 3 次，guest 每项 6 次，列出中位数：

| 工作负载 | 外层原生 | 默认内核 | 实验内核 |
| --- | ---: | ---: | ---: |
| 100000 次 getpid 系统调用 | 0.0614 | 0.5055 | 0.5424 |
| 100 次 fork/exec | 0.0367 | 0.4529 | 0.4240 |
| 1 个计算线程 | 0.3738 | 0.3488 | 0.3523 |
| 同一进程的 4 个计算线程 | 0.4002 | 1.4227 | 1.4390 |
| 1 个计算进程 | 0.3570 | 0.3599 | 0.3563 |
| 4 个计算进程 | 0.4382 | 0.4185 | 0.3877 |

这是原生进程与 UML 内进程的比较，不是完整原生 Docker 对照；资源限制不完全相同，也没有覆盖存储、网络及 GPU 性能。小幅高低差不能推导整体加速。系统调用、fork/exec 和共享地址空间线程的显著差距仍存在。原始数据及程序身份校验见 `native-comparison.json` 和 `benchmark-sha256.txt`。

实验内核上连续运行两轮原版 Docker 29.8.2 子集测试，结果都为 **27 pass / 1 fail / 2 not_tested**。覆盖的 bridge DNS、guest 内端口发布、IPv6、BuildKit、OOM、PID 限额、信号等测试通过；GPU 计算失败，对外网络和完整兼容性未覆盖。见 `docker-cow-1/2.json` 及对应日志。`suite-evidence-validation.json` 使用加强后的解析器重新确认两轮均有完整的 30 个结果、失败结束状态，且不能判为通过。

第一次复用上轮磁盘时，同名网络和容器残留导致额外的 bridge/端口/IPv6 失败，该失败证据保留为 `docker-dirty.json/log`。之后运行器使用独立 COW 层与干净基础镜像，两轮相关功能均恢复。没有把第一次失败删去，也没有通过删除失败用例获得通过。

`native-cuda.jsonl` 记录外层四张 A100 各自分配显存、执行 PTX、传输并校验 1048576 个元素。内层仍因缺少 GPU 后端在 `docker run --gpus all` 失败。外层成功不能替代内层验收。
