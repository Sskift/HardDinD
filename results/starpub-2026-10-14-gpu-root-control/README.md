# NVIDIA RM_CONTROL 根查询实测

实验补丁 `0019-um-gpu-root-control.patch` 在真实 UML 内核中增加四个 NVIDIA 550.120 根查询：设备枚举、设备信息、PCI 信息和 GPU UUID。继续使用已有 RM 客户端和专用 worker；不包含设备对象分配、mmap、UVM、CUDA 或 `--gpus all`。

NVOS54 外层参数先复制到 guest 内核，固定命令、长度和 flags，避免另一个线程改写外层参数后改变实际宿主操作。四种子结构没有第二层指针或 FD；guest 页预取并同步到共享宿主地址空间后，真实驱动直接访问原地址。预取遇到保护错误时仍让驱动处理：实测嵌套缓冲区不可读或只读时，原生返回 `ioctl=0, RM status=30`；外层参数只读则返回 `EFAULT`。不能把这两层错误合并。

不可变内核 SHA256：`7edf5553b629a4fdcd25922d3dc80e2431c323f5cadcdbe199eb631d2258b9db`。固定 Linux 7.2.10、NVIDIA 550.120 ABI，沿用 `sources.lock.json`；构建清单、实际源码快照和原始日志一并保留。

| 实验 | 实际结果 |
| --- | --- |
| `gpu-root-control-v1` 原生前后 | 两次均 17/17；各 98 次真实设备 ioctl |
| 同轮 guest | 17/17；98 次 ioctl 全部来自 5 个 GPU worker；4 张卡信息和 8 组错误／成功返回逐项匹配原生 |
| GPU 桥默认关闭 | 节点不存在，零设备 ioctl |
| `gpu-root-control-docker-v1` | 12 项外层检查通过；单容器、容器重启、双容器并发、实例重启后五次探针各 17/17，均匹配原生 |
| 旧 REGISTER_FD 回归 | 原生前后及 guest 各 24/24，关闭阶段通过 |
| 旧 RM 客户端回归 | 原生前后 29/29，guest 28/29；原有 `invalid_class_status` 失败仍保留 |
| 线程／映射回归 | 36 个采样和全部语义检查通过，正常退出 |

Docker 同时检查没有设备时拒绝访问、非 root 权限、只读设备限制、配置不匹配拒绝、实际 memfd 后端、输入哈希和正常关机。原生、guest 和两次 Docker 实例共 17 个进程组在结束后均无存活或僵尸进程。构建源码和补丁哈希匹配，20 个已有单元测试通过。

**完整 GPU 和性能验收仍失败。** 本轮查询的成功不代表计算、传输或显存生命周期完成。极端地址、并发 remap、设备对象、更多控制命令和取消语义仍需验证；当前等待还会关闭一个 guest CPU 的中断，不能开放可能长期等待的驱动调用。core 数据只用于功能回归，测量期间其他代理仍在开发，未进行性能对照。

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name gpu-root-control-new \
  --rm-control --memfd-ram --memfd-stub
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --probe /root/harddind-rebuild/results/gpu-root-control-new/gpu-guest-probe \
  --native-baseline /root/harddind-rebuild/results/gpu-root-control-new/report.json \
  --name gpu-root-control-docker-new --rm-control --memfd-ram --memfd-stub
```

另有实际 tmpfs 预检：`/dev/shm` 为 32 GiB，但带 `noexec`，共享可执行映射返回 `EPERM`；`/dev` 可执行但只有 64 MiB。现有大容量 tmpfs 不能直接作为 2 GiB UML 的可执行内存文件后端，未运行无效的 tmpfs 性能对照，也未改变宿主挂载权限。详见 `tmpfs-preflight-v1.json`。
