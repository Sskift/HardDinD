# GPU worker 的可调度等待

原先 GPU ioctl 通过宿主 futex 等待，整个等待期间关闭所在 guest CPU 的中断。补丁 `0017-um-gpu-async-wait.patch` 增加默认关闭的异步完成通道：保留每个 guest mm 的固定宿主 worker 和串行请求锁，提交后让调用任务睡在 guest completion 上，worker 完成后通过控制 socket 触发 guest IRQ。调度器与其他任务因此能在等待期间运行。

worker 必须先把 futex 状态交还内核，再发送完成字节，避免下一次提交被旧状态覆盖。发送失败会终止 worker，SIGCHLD 路径以 `ECHILD` 唤醒等待者；完成提前于入睡不会丢失。完成通知的 `write` 仅允许写控制 fd 0。共享 payload 保持 128 字节，stub 代码为 2063 字节，仍在一页内。

| 验证 | 结果 |
| --- | --- |
| `host-model` | 使用同一 futex 原语完成 50000 次有序往返，无丢失完成；只是协议模型 |
| `gpu-async-build-v1` | 异步模式下，原生前后和四 CPU guest 各 30/30，24 组原始数据一致；五个实际 worker 均注册完成 IRQ |
| `gpu-async-schedule-v1` | 同一内核按同步／异步／同步／异步运行相同人工延迟；两次同步均出现预期的调度停顿，两次异步通过 |
| `gpu-async-death-v1`、`v2` | 观察到请求提交后停止并终止该实验 worker，guest 调用返回 `ECHILD` 并正常退出；v2 在终止前再核对进程组、启动时间和停止状态 |
| `gpu-async-docker-v1` | 五次探针及功能检查通过，但从控制台找不到 worker 消息，异步启用核验失败；保留原始失败 |
| `gpu-async-docker-v2` | 改为读取本实验 guest 的内核日志，确认 25 个 worker 均使用异步通道；五次探针各 30/30，12 项检查通过 |
| `cuda-async-v1` | 真实内部 CUDA 使用异步 worker，仍在未接入的 `RM_CONTROL 0xd04` 失败，错误 304；原生前后四卡计算通过 |
| `gpu-async-fd-v1` | 异步描述符关联探针原生前后与 guest 各 24/24 |
| `gpu-async-off-v1` | 同一个内核关闭异步选项，原路径各 30/30，未出现异步 worker 注册 |
| `gpu-async-core-v1` | 四 CPU 线程语义与 18 个微基准样本完成，只作功能回归 |

调度诊断限定一个 guest CPU，让定时线程无法绕到另一颗 CPU，以区分等待方式；这不是把产品目标降为单核。每轮执行 20 个真实根客户端分配／释放 ioctl，并在 worker 内加入相同的约 0.1 秒人工延迟。同步路径最大定时停顿为 147792、125526 微秒，异步路径为 44802、21275 微秒。50 ms 是此人工延迟实验的辨别条件，**不是用户认可的性能阈值**。这些结果不能证明真实 GPU 或 Docker 已接近原生性能。

死亡测试也是诊断：故意终止受控 worker 后，根客户端操作按预期失败，不能计为正常 GPU 功能通过。v2 观察到 206 ms 左右的调用阶段内定时任务继续运行，随后内核正常关机。该机制没有实现取消一个永久卡在宿主驱动中的 ioctl；普通 guest 信号不会放弃仍在使用的参数和描述符。

运行时压低了控制台日志级别，因此完成通道证据通过一个只读诊断容器读取 guest 的 `dmesg`。该容器仅增加内部 guest 的 `SYSLOG` 能力，读取的 `/bin`、`/lib` 也来自该实验 guest；未改变外层 Starpub 权限。

内核 SHA256：`ec7c521783615a20a070c319765aca81a2b4e610f43b80865d9ee932cd555f8f`。首次构建缺少 futex 头文件的错误与修复后日志均保留。最终 20 项 Python 单元测试通过；33 个实验进程组无存活或僵尸进程，源码、补丁和内核哈希一致。

```sh
make kernel-gpu-async-experiment WORK=/root/harddind-rebuild
# 先复制 thread-build/linux，后续使用不可变内核。
python3 scripts/run-gpu-async.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name async-scheduling-new
python3 scripts/run-gpu-async.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name async-death-new --kill-worker
python3 scripts/harddind.py /root/harddind-rebuild example start \
  --kernel /path/to/immutable/kernel \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --gpu-control-experiment --gpu-async-wait
```

`run-gpu-control.py`、`run-gpu-control-docker.py` 和 `run-gpu-cuda.py` 接受 `--async-wait`，并核验实际 worker 注册。普通 GPU 构建不包含此实验补丁；即使编入，没有 `gpu_async_wait=1` 也保持同步路径。worker 创建、设备打开与请求提交仍有同步部分。

这一改动没有扩大 GPU ioctl 覆盖。完整 CUDA、设备映射、UVM、显存生命周期、多卡并发、取消语义、内部隔离与原生性能对照仍未完成；完整验收继续失败。日志和源码快照均可复核，未收录磁盘、可执行文件、驱动库或 CUDA 缓存。

初次完整性核对误把两个启动日志的目录遍历顺序当作时间顺序；初版验证结果保留，最终按相同 PID 多重集重新核对，原始日志与运行报告未改动。
