# 共享地址空间 GPU 调用原型

这是 UML GPU 后端实现前的可行性检查，**没有实现内部 Docker 的 GPU 支持**。外层实验不计入内部 GPU 验收，性能也未达标。

## 原型和实际结果

`src/gpu_broker_probe.c` 仅通过当前子进程的 `LD_PRELOAD` 加载到私有 CUDA 探针。它用 `CLONE_VM | CLONE_FILES | SIGCHLD` 创建不同 PID 的处理进程，共享应用的地址空间及文件描述符表。处理进程只执行原始 Linux 系统调用，不调用 libc，避免共享 TLS 导致 errno 或 libc 状态竞争。调用有 30 秒截止时间，进程组由运行器独立回收。

`scripts/run-gpu-broker.py` 串行执行四种模式，均重新创建 CUDA 进程：原生、只转发 NVIDIA ioctl、同时转发设备 mmap、额外尝试第二个设备映射。构建及运行持有实验独占锁，未修改宿主驱动、设备权限、外层 seccomp 或 `/root/udock`。

`gpu-broker-v1` 因编译器的返回值警告未通过构建，四种模式均为 not_tested；修正后 `gpu-broker-v2` 完成以下检查：

| 模式 | NVIDIA ioctl 所在进程 | 设备 mmap 所在进程 | 真实计算 |
|---|---|---|---|
| native | 应用：2725 次 | 应用：128 次 | 4 卡通过 |
| ioctl | 处理进程：2725 次 | 应用：128 次 | 4 卡通过 |
| mmap | 处理进程：2725 次 | 处理进程：128 次 | 4 卡通过 |
| alias | 处理进程：2725 次 | 处理进程：256 次，含第二映射尝试 | 4 卡通过 |

每张卡都创建 CUDA 上下文、分配三块 4 MiB 显存、上传两组数据、执行 PTX 加法、下载并逐项验证 1048576 个元素，再释放显存、模块和上下文。四种模式退出码均为 0，处理进程均正常退出，各专用进程组没有强制回收或残留。strace 核对实际 syscall 的 PID 与原型计数一致，避免把没有经过转发的原生成功误记为成功。

实际驱动为 NVIDIA 550.120。报告记录源码、运行器、两个构建产物、实际加载的 CUDA/JIT 库及原始 trace 的 SHA256；压缩 trace 解压后的哈希已与报告核对。strace 和转发都会改变耗时，因此这里不作 GPU 性能比较。

## 映射边界

第二个映射仅执行 mmap/munmap，没有额外读写 MMIO：

- nvidiactl：116 次第二映射成功。
- nvidia0–3：合计 8 次第二映射成功。
- nvidia-uvm：4 次第二映射全部失败，errno=22（EINVAL）。原始映射和计算仍有效。

alias 模式的 `status=pass` 表示原型完整运行、真实计算及路径核对通过，**不表示 UVM 支持第二映射**；4 次失败在报告的 `alias_failed` 和 `alias_errors` 中保留。

当前 UML 的用户内存复制通过 guest PTE 找到物理页，再从内核别名访问。上述结果说明，可以继续验证普通 NVIDIA 设备映射的双地址映射方案，但不能将它直接套用到 UVM。UVM 需要单独处理 guest 虚拟地址、宿主映射、缺页及内核用户内存访问。

NVIDIA 550.120 的官方定义也明确包含 ioctl 内嵌用户地址和文件描述符：[nv-ioctl.h](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/550.120/kernel-open/common/inc/nv-ioctl.h)、[uvm_ioctl.h](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/550.120/kernel-open/nvidia-uvm/uvm_ioctl.h)。这里验证的是共享 mm 能否让原驱动直接使用这些地址；尚未验证 UML 中的缺页、COW 和文件描述符转接。

## 尚未实现和验证

- UML 内核字符设备、ioctl 分发、设备 VMA 与文件描述符转换。
- 原型使用 CLONE_FILES，实际 UML 执行进程的文件表独立，不能据此省略 FD 转换。
- 不同处理 PID 交错执行、真正并发、fork/exec 和异常退出生命周期。
- UVM、锁页内存、托管内存、guest 内核 copy_to/from_user 与 cgroup 计费。
- 内部 `docker run --gpus all` 的实际工作负载和同机 GPU 性能对照。

复现：

```sh
python3 scripts/run-gpu-broker.py /root/harddind-rebuild --name new-gpu-broker
```

每个结果目录只能新建，原始失败不被重试覆盖。此原型不能作为一般应用的预加载库安装。
