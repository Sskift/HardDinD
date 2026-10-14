# UML 内核 GPU 控制通道

**内部 CUDA 计算仍未实现，完整 GPU 和项目验收仍为 fail。** 本轮把之前的外层调用实验推进到实际 UML 内核：新增 opt-in 补丁 `0012-um-gpu-control-transport.patch`，通过 `/dev/harddind-nvidiactl` 访问真实外层 NVIDIA 控制设备。该节点仅用于接入诊断，不冒充可运行 CUDA 的 `/dev/nvidiactl`。

## 实现

- 每个接触设备的 guest mm 创建独立宿主处理进程，与应用执行进程共享 mm，但保持固定宿主 TGID，不跟随虚拟 CPU 迁移。不同 guest mm 不共享处理进程。一个 mm 中的不同 guest TGID 暂不支持，明确返回 EXDEV。
- 外层 UML 进程只打开固定的 `/dev/nvidiactl`，经现有 SCM_RIGHTS 通道传递文件引用。设备处理进程保留自己的描述符，guest 文件对象持有 mm 引用直到最后关闭。内部 dup 使用真实 Linux 文件引用语义。
- ioctl 使用原 guest 虚拟地址。预先处理可读和可写页并同步 UML 映射；可写预处理使用不修改内容的 `fault_in_safe_writeable`。本轮仅开放 NVIDIA 550.120 的平面控制查询，尚未接入内嵌 FD/指针命令、mmap 或 UVM。
- 设备错误写入独立返回值，不污染映射命令队列。GPU 处理进程纳入退出追踪；接触 GPU 的 mm 不复用映射管理进程缓存。
- 默认不注册设备节点，也不开放新增 stub 设备调用；必须显式启用 `gpu_bridge=1`。控制查询的等待仍会关闭当前 guest CPU 的中断，尚不能用于可能长时间阻塞的 CUDA 命令。原 UML stub 的内部隔离缺口也没有解决。

## 实测

`gpu-control-v3` 使用同一静态探针，依次运行原生前置、未启用的 guest、启用的 guest、原生后置。四阶段均通过且正常退出，未强制清理，无残留进程。内核 SHA256 为 `abb66d3eccac7311a9e31674e4ff5205f53c21cea635e55f19b1070379d4d1e3`。

启用的 guest 中 13 项检查通过：打开、真实驱动版本、跨页参数、非法地址、驱动拒绝错误大小、错误之后恢复、dup、关闭原描述符、通过副本继续调用、4 线程共 128 次查询、关闭最后引用、16 次重开，以及 4 个独立子进程重新打开设备。版本来自真实驱动并与同机原生的 550.120 一致。测试实际使用版本查询 ioctl；其他已允许的平面查询没有计为已验证。

strace 记录 guest 的 153 次真实 NVIDIA ioctl 全部来自记录中的 5 个专用处理进程（主进程及 4 个独立子进程），没有落到按 CPU 分配的应用执行进程。两轮原生各有 154 次；guest 的非法地址在 guest 内核被拒绝，因此少一次外层 ioctl。未启用时没有节点、处理进程或 NVIDIA ioctl。所有原始 trace 压缩保存，解压哈希已与报告核对；源码快照、内核补丁清单、磁盘及探针哈希保留。追踪开销不用于性能结论。

## 保留的失败

- 首次构建使用已移除的 `no_llseek`，编译失败；保存 `build-v1-failed.log/.patch`，尚未运行 guest。移除该字段后默认不可 seek。
- 第二次构建触发编译器生成页外跳转表，stub 重定位检查拒绝通过；保存 `build-v2-relocation-failed.log/.patch`。改成独立条件分支后，第三次构建没有外部重定位。
- `gpu-control-v1` 准备根目录时错误复制 `/dev/zero`，被主动中断；只删除本轮不完整副本并释放空间。四阶段均未测。修复后明确跳过运行时挂载目录，复制函数拒绝读取非普通文件。
- `gpu-control-v2` 的原生与默认关闭检查通过；启用检查失败是 init 从 `/proc/cmdline` 寻找已被 UML 参数处理器移除的开关，实际没有执行探针。v3 按设备节点选择执行路径，验证器独立要求启用模式产生全部检查和真实调用，因此节点缺失不能变成通过。

## 复现

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /path/to/immutable/gpu-kernel --name new-gpu-control
```

构建基于固定 Linux 7.2.10 及现有 IPI 系列补丁；不与未验证的 mprotect 实验叠加。测试使用独立磁盘、进程组和实例名，不操作 `/root/udock`。最初的 guest 控制测试仍缺内部 Docker 验证，后续证据见下文；实际 CUDA、显存与锁页映射、内嵌 FD 转换、并行阻塞请求、进程间 FD 传递及 GPU 性能验收仍未完成。

## 内部 Docker 接入与生命周期

后续 `gpu-control-docker-v1` 完成 7 项检查；扩展的 `gpu-control-docker-v2` 完成 11 项检查，全部通过。持久运行器新增显式 `--gpu-control-experiment` 开关，保存到实例配置并核对重启参数，未启用时保持关闭。

测试使用真实内部 Docker 的 `import`、`run --device`、`--user`、设备只读授权、`start -a` 和 `rm`：不传设备时报 ENOENT，UID 1000 打开 0600 节点时报 EACCES，只给设备读权限而尝试读写打开时报 EPERM。正常授权时完成相同的 13 项控制探针检查；容器再次启动、两个独立容器同时运行、整个 UML 实例正常关闭后重新启动，均完成同一组检查。已运行实例的 GPU 开关不一致时正确拒绝复用。两次 UML 启停均正常，内核、磁盘、探针及运行器输入哈希未变。

```sh
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /path/to/immutable/gpu-kernel --disk /path/to/runtime.ext4 \
  --probe /path/to/gpu-guest-probe --name new-gpu-control-docker
```

这些检查验证了内部 Docker 到真实 NVIDIA 控制设备的通路和该子集的权限行为，**没有运行内部 CUDA，也没有验证显存、UVM、GPU 计算并发或 GPU 性能**。先前缺少的内部控制测试已补上，其余缺口仍保留。

## 通用回归与清理

同一个新内核在默认关闭 GPU 通道时完成 `gpu-control-core-v1`：3 组映射语义、5 组线程语义以及 12 类工作负载的全部 36 个样本通过，包括写保护、固定映射替换、私有 COW、fork/文件截断、TLS、CPU 迁移和并发页表回收。没有开启 mprotect 实验。此轮允许使用宿主 CPU 0–71，未配套原生或旧内核性能对照，耗时有明显波动；不得把回归通过写成性能不退化或接近原生。

核心回归退出时，清理器曾观察到一个处于 D 状态、等待文件系统写入访问的 `uml-userspace` 进程，随后在清理等待期间自行退出，未强制终止。该记录保留，不据此关闭先前的偶发退出问题。最终独立审计覆盖 14 个实验进程组和已记录的 GPU 处理进程，均无残留。原 `/root/udock` 只读健康检查仍返回 Docker 29.8.2、运行中容器 0、镜像 6。
