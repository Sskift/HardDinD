# 共享地址空间 GPU 调用原型

这是 UML GPU 后端实现前的可行性检查，**没有实现内部 Docker 的 GPU 支持**。外层实验不计入内部 GPU 验收，性能也未达标。

## 原型和实际结果

`src/gpu_broker_probe.c` 仅通过当前子进程的 `LD_PRELOAD` 加载到私有 CUDA 探针。默认用 `CLONE_VM | CLONE_FILES | SIGCHLD` 创建不同 PID 的处理进程，共享应用的地址空间及文件描述符表；后续增加了同 TGID 的线程模式。处理者只执行原始 Linux 系统调用，不调用 libc，避免共享 TLS 导致 errno 或 libc 状态竞争。调用有 30 秒截止时间，进程组由运行器独立回收。

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

v2、v4 的第二个映射是在同一个共享 mm 中选择另一个虚拟地址，仅执行 mmap/munmap，没有额外读写 MMIO。这两轮没有验证另一个宿主 mm；后续 v6 的独立 mm 检查见下文：

- nvidiactl：116 次第二映射成功。
- nvidia0–3：合计 8 次第二映射成功。
- nvidia-uvm：4 次第二映射全部失败，errno=22（EINVAL）。原始映射和计算仍有效。

alias 模式的 `status=pass` 表示原型完整运行、真实计算及路径核对通过，**不表示 UVM 支持第二映射**；4 次失败在报告的 `alias_failed` 和 `alias_errors` 中保留。

当前 UML 的用户内存复制通过 guest PTE 找到物理页，再从内核别名访问。上述结果说明，可以继续验证普通 NVIDIA 设备映射的双地址映射方案，但不能将它直接套用到 UVM。UVM 需要单独处理 guest 虚拟地址、宿主映射、缺页及内核用户内存访问。

NVIDIA 550.120 的官方定义也明确包含 ioctl 内嵌用户地址和文件描述符：[nv-ioctl.h](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/550.120/kernel-open/common/inc/nv-ioctl.h)、[uvm_ioctl.h](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/550.120/kernel-open/nvidia-uvm/uvm_ioctl.h)。这里验证的是共享 mm 能否让原驱动直接使用这些地址；尚未验证 UML 中的缺页、COW 和文件描述符转接。

## 线程组对照：不同 PID 失败，同 TGID 成功

`gpu-broker-v3 --brokers 2` 将调用按顺序交替分配到两个独立进程，原生对照通过，但转发模式在创建第一个 CUDA 上下文时返回错误 800，执行了 354 次 ioctl 后退出。后续 mmap、alias 模式保持 not_tested，没有继续使用失败初始化的状态。

随后增加 `--threads`：使用 `CLONE_VM | CLONE_FILES | CLONE_SIGHAND | CLONE_THREAD | CLONE_PARENT_SETTID | CLONE_CHILD_CLEARTID`，由宿主内核清除线程 TID 并通知退出。原始处理线程屏蔽可屏蔽信号，不运行应用的 libc 信号处理器；它们不拥有独立的 libc TLS，也不调用 pthread API。原型仍按调用串行调度，不是并行性能测试。

| 结果 | 处理者 | 原生对照 | ioctl 模式 | mmap 模式 | alias 模式 |
|---|---|---|---|---|---|
| v3 | 两个不同 TGID 的进程 | 4 卡通过 | CUDA 800，失败 | 未测 | 未测 |
| v4 | 同 TGID 的两个线程 | 4 卡通过 | 4 卡通过 | 4 卡通过 | 4 卡通过，UVM 别名仍失败 |
| v5 | 同一份 v4 二进制，切回不同 TGID | 4 卡通过 | CUDA 800，失败 | 未测 | 未测 |

v4 与 v5 的 CUDA 探针、转发库、源码和运行器哈希完全相同，转发库 SHA256 为 `4b3a8d672ad7322c959cf0f891afd49ae11e331e479499dae5a70698c958b47a`。唯一配置差异是线程模式。v4 的 ioctl 模式分别由两个线程执行 1363 和 1362 次 ioctl；mmap 模式两个线程分别执行 64 次设备 mmap，strace 同时确认创建时确实使用了 CLONE_THREAD。所有已运行模式的进程组正常清理，没有强制回收。

错误 800 在 NVIDIA 文档中为 [CUDA_ERROR_NOT_PERMITTED](https://docs.nvidia.com/cuda/archive/12.0.1/cuda-driver-api/group__CUDA__TYPES.html)。这些对照支持“当前工作负载需要一致的宿主线程组身份”的设计约束；**没有定位到具体触发拒绝的驱动源码检查**，也没有证明任意驱动版本或 CUDA 程序都有相同行为。

UML 目前的每 CPU 执行进程有不同 TGID，因此不能直接把一个 CUDA 客户端的调用随意分发给这些执行进程。后续内核接入应验证按 guest 线程组绑定一组宿主 GPU 处理线程，同时保留正确的 mm、文件描述符和生命周期。不能把整个 guest 的所有独立应用合并成一个 GPU 身份，也不能在映射管理进程缓存复用时遗留前一个应用的 GPU 资源。

## 独立地址空间中的新映射

`gpu-broker-v6 --brokers 2 --threads --modes native mmap crossmm` 完成三种模式，每种均验证外层四卡计算、上传、下载和释放，退出正常，无强制清理或残留。crossmm 在每次原始设备 mmap 成功后，以不含 CLONE_VM/CLONE_THREAD 的原始 clone 建立一个独立 mm 的子进程，再在子进程中发起新的 mmap/munmap；子进程仅使用原始系统调用。strace 核对了全部 128 个子进程的创建标志、实际设备 mmap 和退出。

新映射结果：nvidiactl 116 次成功，nvidia0–3 合计 8 次成功，nvidia-uvm 4 次全部 EINVAL。报告显式保留 `all_alias_mappings_supported=false`，不能把诊断模式的 pass 当成全部设备映射成功。每个阶段的原始 trace 解压哈希均与报告一致；本轮还保存参与编译的完整源码快照。

这证明该探针能够在另一个宿主 mm 中重新建立普通设备映射，但子进程仍继承父进程的文件引用和地址空间初始内容，且没有读取或写入新映射。**尚未验证独立启动的 UML 内核进程接收 FD 后的映射、数据一致性或 guest 内核用户内存访问**。UVM 的完整映射语义、锁页和缺页路径仍未解决；GPU 验收仍为 fail。

后续核对固定版本的 [NVIDIA 550.120 uvm_mmap](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/550.120/kernel-open/nvidia-uvm/uvm.c#L695-L722) 发现，该入口要求映射起始地址等于文件偏移，否则返回 EINVAL；启用 mm 绑定时，还会拒绝其他 mm。原始 trace 中 UVM 的地址和偏移均为 `0x206200000`，长度 2 MiB；别名试验保留偏移却让内核选择新地址，违反了前一约束。源码中的检查解释了这种请求为何会被拒绝，但没有对已加载驱动插桩确认实际拒绝分支。不能把这几次 EINVAL 推广为“任何跨 mm UVM 方案均不可行”；也不能只把偏移改成新地址，就认为映射了原来的内存。真实 UVM 接入仍需处理原虚拟地址、缺页及 guest 内核访问。

## 尚未实现和验证

- UML 内核字符设备、ioctl 分发、设备 VMA 与文件描述符转换。
- 原型使用 CLONE_FILES，实际 UML 执行进程的文件表独立，不能据此省略 FD 转换。
- 不同处理 PID 交错执行已失败；同 TGID 的串行交错子集通过，真正并发、fork/exec 和异常退出生命周期仍未验证。
- 普通设备跨 mm 别名在实际 UML 内核中的接入与数据访问、UVM、锁页内存、托管内存、guest 内核 copy_to/from_user 与 cgroup 计费。
- 内部 `docker run --gpus all` 的实际工作负载和同机 GPU 性能对照。

复现：

```sh
python3 scripts/run-gpu-broker.py /root/harddind-rebuild --name new-gpu-broker
python3 scripts/run-gpu-broker.py /root/harddind-rebuild --name new-gpu-threads --brokers 2 --threads
python3 scripts/run-gpu-broker.py /root/harddind-rebuild --name new-gpu-crossmm --brokers 2 --threads --modes native mmap crossmm
```

每个结果目录只能新建，原始失败不被重试覆盖。此原型不能作为一般应用的预加载库安装。
