# 真实 CUDA 设备发现与设备号修复

这一轮运行原有完整 `cuda_probe.c`：每张 GPU 分配三块显存、上传数据、执行 PTX 加法、下载并核对 1048576 个结果，然后释放资源。相同程序在 Starpub 外层的 4 张 A100 上通过；内部 Docker 仍在 `cuInit` 失败，**没有完成内部 GPU 计算验收**。

真实 CUDA 暴露了此前诊断探针未覆盖的问题：`/dev/harddind-nvidiactl` 原来是杂项设备，设备号为 `10:260`。即使 Docker 将它映射为 `/dev/nvidiactl`，NVIDIA 用户态库也会检查设备号，删除不匹配的节点，重建为 `195:255`；容器只获准访问原来的设备号，因此随后 `open` 返回 `EPERM`。

`cuda-pointer-v4/docker.strace.gz` 保存了完整的 `stat → unlink → mknodat → open EPERM` 过程。缺少 `/sys/module/nvidia/initstate` 和 `/proc/driver/nvidia/params` 也出现在 trace 中，但不能只看到这些缺失就把它们当成此次失败原因。

补丁 `0021-um-gpu-device-numbers.patch` 将实验节点改为来宾内核自己的 cdev，采用 NVIDIA 的 `195:minor` ABI，控制节点为 `195:255`。仍保留诊断节点名称和默认 `0600` 权限，GPU 桥仍须显式启用；没有改变外层设备规则或 Starpub 的宿主驱动。

| 证据 | 结果 |
| --- | --- |
| `cuda-pointer-v1` | 诊断脚本把缺失 UVM 节点的 CLI 返回值误认为应是 125，实测为 127，提前停止；原始失败保留 |
| `cuda-pointer-v2` | 原生前后均四卡计算通过；内部 `cuInit` 返回 100，尚无有效设备 ioctl |
| `cuda-pointer-v3` | 增加真实 guest syscall trace，确认 `/dev/nvidiactl` 打开返回 EPERM |
| `cuda-pointer-v4` | 扩大文件 syscall 记录，确认设备号检查与节点重建是原因 |
| `cuda-device-numbers-v1` | 修复后不再重建设备，成功打开 `195:255`，CARD_INFO 和 RM_ALLOC 成功；下一个 RM_CONTROL 仍失败 |
| `gpu-device-numbers-fd-v1` | 原生前后与 guest 各 24/24，默认关闭阶段通过 |
| `gpu-device-numbers-docker-v1` | 五次描述符探针各 24/24，12 项权限／并发／重启／清理检查通过 |

新内核 SHA256：`2db3aff73e9809f46684e8a928d6ffb892e86d393aac3292d2f7525c79cac3df`。构建与 stub 重定位检查通过；31 个实验进程组在结束后没有存活或僵尸进程，源码与补丁清单一致。非 root 与只读设备限制依然生效。

下一处已确认的缺口是 `RM_CONTROL 0x101`（`NV0000_CTRL_CMD_SYSTEM_GET_BUILD_VERSION`）：40 字节子结构中还有三个输出字符串指针。当前只支持四种不含更深指针的根查询，因此明确返回 `EOPNOTSUPP`。`cuda-device-numbers-v1/docker.stderr.gz` 同时保存请求参数和返回值；错误没有被伪装成成功。UVM 节点、设备映射、更多设备对象和可调度等待仍未完成。

新增 `scripts/run-gpu-cuda.py` 在独立实例中打包实际外层 NVIDIA 库、运行原生前后对照和内部计算探针，并记录库／程序／内核哈希、逐命令退出状态与清理情况。`src/cuda_ioctl_trace.c` 只记录实际 ioctl；对于已编码长度的 NVIDIA 参数，最多读取 64 字节且不越过编码长度，未知长度的 UVM 只记录请求元数据。`diagnostic_complete` 表示观察流程结束，不等于 CUDA 通过；内部失败时运行器返回非零。第一个保留版本的 logger 曾读取最多 64 字节，后续已收紧到实际编码边界。

```sh
python3 scripts/run-gpu-cuda.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --name cuda-device-new
```

此入口使用显式设备映射诊断后端，没有实现 `docker --gpus all` 的集成。报告中的计算耗时受追踪影响，不作为性能对照；完整 Docker、GPU 与接近原生性能的目标均保持未完成。标准输出、错误输出和 syscall trace 以可解压的 `.gz` 保存，未收录磁盘、驱动库、可执行文件或 CUDA 缓存。
