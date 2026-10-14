# NVIDIA 描述符关联与 UML 用户内存权限修复

**内部 CUDA 仍未实现，完整 Docker、原生性能和 GPU 验收仍未通过。** 本轮在真实 UML 内核接上 NVIDIA `NV_ESC_REGISTER_FD`，并修复实测发现的通用 `copy_from_user` 权限缺陷。没有修改宿主驱动、外层权限或 `/root/udock`。

## 实现与边界

- 实验补丁 `0013` 在启动时发现已有 `/dev/nvidia0..254` 和控制设备，为存在的设备注册 `harddind-nvidia*` 诊断节点。Star­pub 实测为四张 GPU。设备文件经固定路径打开并传入每个 guest mm 的专用处理进程；不允许用户提供任意宿主路径。
- `REGISTER_FD` 的 guest 描述符通过持有的 Linux 文件引用转换到同一处理进程的 host 描述符。参数使用独立 stub 字段传递，不临时篡改用户缓冲区，不把用户提供的整数直接当作 host FD。错误来源描述符转换为无效 host FD，交给真实驱动执行其状态检查；跨 mm 传递明确返回 EXDEV。
- 驱动执行后回写原 guest FD，保留 NVIDIA 550.120 的顺序：参数不可读时不执行注册；参数可读但不可写时，可能已经注册，然后返回 EFAULT。关闭源描述符后，已注册目标继续由驱动持有控制设备引用。
- 默认内核补丁 `0014` 修复 `arch/um/kernel/skas/uaccess.c`：`pte_present()` 对 `PROT_NONE` 也返回真，原路径据此直接从物理页复制，漏掉读取权限。现在同时检查 `pte_read()`，不允许绕过保护。此修复作用于普通用户内存读取，不局限于 GPU 接口。
- 原控制补丁允许的 `SYS_PARAMS` 命令 214 实际会设置外层全局 NUMA 参数，因此从诊断允许列表移除；没有为测试调用该命令或修改这个宿主状态。ABI 参考固定在 NVIDIA 550.120 提交 `5e52edb2034de7db4d8ae368dbc7c26b416bfa16`，记录于 `sources.lock.json`。

仍不支持 CUDA、设备 mmap、UVM、锁页内存、通用内嵌指针/FD 转换、跨进程继承或传递设备描述符。设备发现是启动快照，不含热插拔。当前设备等待仍关闭一个 guest CPU 的中断，不能据此开放可能长时间阻塞的 GPU 请求；UML stub 的内部隔离缺口也没有解决。诊断节点仍默认关闭，不是 `--gpus all` 后端。

## 原生与 guest 实测

`gpu-fd-native-v1` 首先确认原生 21 项驱动行为。`gpu-fd-v1` 的原生和关闭设备检查通过，guest 出现参数读取失败却产生注册副作用，原生后置未测。加入普通管道检查后，`gpu-fd-v2-before-uaccess` 在同一旧内核稳定复现两项失败：从 `PROT_NONE` 页向管道写入竟然成功，以及 GPU 注册副作用；其他检查没有替代这些失败。

修复后的 `gpu-fd-v3-after-uaccess` 四阶段均通过：原生前置、默认关闭、启用 guest、原生后置。前后使用同一份静态探针，旧／新内核对照的探针 SHA256 也一致。启用时 24 项通过，覆盖：普通管道的读／写权限与错误后数据保留；高编号 FD（至少 200）；无效 FD、非 GPU FD、错误源设备、自引用、重复关联与控制设备关联链；源关闭、目标引用保留；四 GPU 设备注册；参数读入／回写故障；四线程、16 次重开和四个独立进程重新打开。

原生每轮 41 次 NVIDIA ioctl，guest 为 40 次：非法读取在 guest 内核被拒绝，未到达外层驱动。guest 的实际 ioctl 全部来自记录中的五个专用处理进程。三轮诊断的退出均正常，无强制清理；失败与未测状态完整保留。

- 修复前内核：`d552b594b5447f0fb91d9878cede70994cc354c7be7988751228b03df5d43e9a`
- 修复后内核：`999505d9e7b04864abda446e92d0f9edbf9fbe7a073d6d30fc91e1d531e30aed`

构建第一次因远端 Makefile 尚未同步而未执行；同步后构建成功。保留全部三份构建日志，后两次编译和 stub 页外重定位检查均成功。源码快照、内核补丁清单、输入哈希、日志和压缩原始 trace 一同保存。这些带追踪的功能检查不提供性能结论。

## 实际内部 Docker

`gpu-fd-docker-v1` 的 11 项检查全部通过：缺少设备、非 root、设备只读授权均正确拒绝；正常设备接入、容器再次启动、两个独立容器并发、整个 UML 实例重启后，各执行全部 24 项检查；配置开关不匹配被拒绝；输入哈希不变；两次实例关机正常，未强制清理。测试使用真实 Docker 的 import、run、--device、--user、start 和 rm。

四张 GPU 的“注册通过”仅证明驱动的设备关联语义，不代表四卡计算、显存访问或多卡性能通过。子进程测试各自重新打开设备，没有把不支持的继承描述符使用计为通过。

## 复现

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
# 将成功构建的内核复制为独立、不可变的实验路径后运行：
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /path/to/gpu-fd-kernel --name new-gpu-fd \
  --fd-registration --gpu-count 4
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /path/to/gpu-fd-kernel --disk /path/to/runtime.ext4 \
  --probe /root/harddind-rebuild/results/new-gpu-fd/gpu-guest-probe \
  --name new-gpu-fd-docker --fd-registration --gpu-count 4
```
