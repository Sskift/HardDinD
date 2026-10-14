# NVIDIA RM root-client 实测

实验补丁 `0016-um-gpu-rm-client.patch` 在原控制查询、REGISTER_FD 基础上增加真实 NVIDIA RM 根客户端分配与释放。固定 NVIDIA 550.120 ABI；仍只开放诊断设备节点，没有设备 mmap、UVM、CUDA 或 `--gpus all` 支持。

参数先复制到 guest 内核，验证结构和允许的根客户端类别，再通过 64 字节共享缓冲区交给原有专用 GPU worker。32 字节 NVOS21 与 48 字节 NVOS64 分别读取正确偏移的参数长度；非空嵌套指针和未开放类别拒绝转发。16 字节 FREE 的数字句柄交给真实驱动检查，保留 ioctl 返回值和内嵌 RM status 两层错误语义。此前补丁 `0012/0013` 未改写。

不可变内核 SHA256：`ae7ed172388f4e251d9b93994511d3887fc94465a42d2fabc516f15f2c713e00`。内核构建、共享区大小断言和 stub 外部重定位检查通过。

| 实验 | 实际结果 |
| --- | --- |
| `gpu-rm-v1` 原生前后对照 | 两次均 29/29；各 205 次真实 NVIDIA ioctl |
| 默认关闭 GPU 桥 | 诊断节点不存在，零设备 ioctl，通过 |
| 启用桥的 guest | 28/29；201 次真实设备 ioctl 全部来自 5 个专用 worker |
| `gpu-rm-docker-v1` | 单容器、容器重启、两个并发容器、实例重启后五次探针，均 28/29 |

**剩余失败没有豁免**：未知对象类别在原生驱动返回 `ioctl=0`、内嵌 `NV_ERR_INVALID_CLASS`，当前只允许根客户端类别的桥返回 `EOPNOTSUPP`。因此 standalone 总报告仍失败；Docker 的五项完整探针检查也都失败，总计 7 pass、5 fail。没有在桥中伪造驱动状态，未删除探针检查。

已通过的探针覆盖两种 ALLOC 结构、三种根类别别名、分配/释放、并存句柄、重复与非法释放、跨 FD 属主校验、错误恢复、四线程循环、关闭 FD 后回收和四个独立子进程。它们验证的是 RM 客户端生命周期，**不是显存生命周期或 GPU 计算**。

Docker 另外验证了无设备、非 root 和只读 cgroup 设备限制、配置不匹配拒绝、实际 memfd 后端及输入不变。两轮实例均正常关闭。原生/guest 四阶段均正常退出且输入哈希未变。报告、逐命令日志、压缩 syscall trace、构建记录及实际源码快照全部保留。

当前 worker 交接仍在一个 guest CPU 关闭中断期间等待；不能据此宣称任意驱动调用有时限，也不应在该路径直接开放可能长期等待的 CUDA 命令。后续需要可中断等待、设备对象与嵌套 FD/指针翻译、相同地址的 GPU/UVM 映射，再做内部计算、传输、多卡和内存生命周期验证。

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name gpu-rm-new \
  --rm-client --memfd-ram --memfd-stub
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --probe /root/harddind-rebuild/results/gpu-rm-new/gpu-guest-probe \
  --name gpu-rm-docker-new --rm-client --memfd-ram --memfd-stub
```

回归：`gpu-rm-fd-regression-v1` 四阶段全部通过（有效阶段各 24 项），线程/映射 `gpu-rm-core-v1` 完成 36 个采样及全部语义检查，正常退出。该次 core 在代理开发和外部负载仍存在时运行，仅作功能回归，不用于推断性能变化。清理审计核对 13 个进程组无存活/僵尸，内核源码与补丁清单一致；20 个单元测试及 Python 编译检查通过。`/root/udock` 只读健康检查仍为 Docker 29.8.2、0 个运行容器、6 个镜像。
