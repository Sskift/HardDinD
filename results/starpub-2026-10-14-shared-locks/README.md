# 共享文件锁：真实 Docker 对照与内核取消通路

当前未完成。旧内核的 `shared-flock-v1` 为原生 21 项通过、内部 Docker 17 项通过及 4 项失败；新内核的 `shared-flock-v2` 为原生 21 项通过、Docker 19 项通过及 2 项 POSIX 范围锁失败。两轮程序均退出 1。不能用这组文件锁检查代替完整 Docker、GPU 或性能验收。

## 实测结果

`virtiofs/patches/0002-shared-flock.patch` 将 flock 绑定到外层打开文件描述，保留 dup/fork 的共享关系；独立等待队列重试非阻塞锁，避免阻塞请求占满普通文件工作线程。实际覆盖：双向排他、共享读锁、独立打开、dup/fork 最后关闭、正常关闭与进程退出释放、双向阻塞、重命名和删除后的锁、降级和升级失败、只读 bind mount。6 个等待者超过 4 个工作线程时，普通读和解锁仍能完成。

失败项保留在 JSON 和日志中：

| 检查 | 原生 | Docker | 观察 |
|---|---|---|---|
| SIGUSR1 取消锁等待 | pass | fail | 等待响应超时 |
| SIGKILL 取消锁等待 | pass | fail | docker kill 超时，等待锁的进程没有及时结束 |
| 外层 POSIX 范围锁排斥内部 | pass | fail | 内部仍能取得锁 |
| 内部 POSIX 范围锁排斥外层 | pass | fail | 外层仍能取得锁 |

服务端已实现 FUSE interrupt 接收，但固定 Linux 7.2.10 的 `fs/fuse/virtio_fs.c:virtio_fs_send_interrupt` 仍是 TODO 空实现，阻塞请求的取消消息没有发出。本轮服务退出记录 `completed=6020 interrupted=0 peak=7 remaining=0`。POSIX 字节范围锁尚未实现，不能由 flock 通过推断支持。

实例正常关机，guest、网络与文件服务的退出码均为 0，没有强制清理或残留进程。62 个探针进程组的检查均为 clean_shutdown。取消测试失败后，测试释放外层锁才让请求退出；最终清理成功不代表取消语义通过。本轮未重新检查 `/root/udock` 的在线健康状态，也未修改它。

## 性能

同一程序在 CPU 0–3 上执行 1000 次无竞争排他加锁/解锁；原生前置 3 次、Docker 两组共 6 次、原生后置 3 次。CPU 不独占。

| 模式 | 中位耗时 |
|---|---:|
| 原生 | 0.001550038 秒 |
| Docker | 0.253156992 秒 |

这个微基准约慢 163 倍，尚未接近原生。它不代表所有应用都会慢同样倍数，也不构成完整性能比较。每次远端锁经过 guest、virtiofs 服务和外层系统调用，是后续需要分段测量的路径；当前数据尚不能把总耗时精确分摊到各段。

## 新内核补丁的证据边界

`kernel/patches/0011-fuse-virtio-interrupts.patch` 增加优先队列上的 FUSE_INTERRUPT 请求、独立原请求引用、EAGAIN 重试，以及信号早于 FR_SENT 时的补发。请求构造在可睡眠工作队列中进行，避免在提交锁内分配内存。原请求先结束和取消回复后到的情况保留单独引用处理。

该补丁在 Starpub 上以 `IPI_EXPERIMENT=1` 构建成功，链接及 stub relocation 检查通过，构建进程退出码 0。首次构建被补丁路径白名单拒绝；白名单现在仅增加 `fs/fuse/`，新增路径和符号链接逃逸回归通过。两次构建日志都保留。

构建完成后，SSH 跳板一度不可用。随后通过本机已有 SOCKS 通道恢复原 SSH 端口连接，并保留主机密钥校验；没有修改宿主权限或网络配置。新内核复制到独立不可变路径，SHA256 为 `c53fc2a0b949143109281151d3073c2ac9a18aff976f7345d43dfe0cfeba8403`。

`shared-flock-v2` 的 SIGUSR1 与 SIGKILL 取消等待两项均通过，Docker 共 19 项通过，两个 POSIX 范围锁检查仍失败。服务日志记录 `completed=6026 interrupted=2 peak=7 remaining=0`。guest、网络和文件服务均正常退出，62 个探针进程组都没有强制清理或残留；新内核的这次正常关闭没有挂起。设备重置、异常恢复、取消竞态压力、POSIX 锁、共享 mmap、通知和 GPU 仍待验证或实现。

本轮 1000 次加锁/解锁中位耗时为原生 0.001825501 秒、Docker 0.529507968 秒（约 290 倍）。这比第一轮的比值更差，两轮未做交错内核 A/B，也未控制共享宿主负载，不能将差值直接归因于内核补丁。功能修复没有证明性能改善，后续需要分段计时。

## 复现

先按固定版本构建内核、virtiofsd、Moby、网络和共享探针；运行时使用不可变内核副本和独立名称，禁止覆盖已运行实例的输入：

```sh
python3 scripts/run-shared-locks.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/virtiofs-kernel-v1 \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --name new-lock-run
```

旧内核 SHA256 为 `bedfe12f0d12960de45aa6e2718679dcb919841e4cfb9d9bc697bf84c8d5fb4b`；服务 SHA256 为 `c3352876265043b297cd3f4f6be95866ffbb146c49a11b4a17d946e661e54665`。完整脚本、磁盘、探针哈希与原始性能样本见 `shared-flock-v1.json`。原生只读基线使用 O_RDONLY，Docker 侧使用实际只读 bind mount，两者不完全相同。

## 分段诊断工具

`virtiofs/patches/0003-request-profile.patch` 增加默认关闭的 `--profile-requests`。运行器通过 `--share-profile` 启用；测试脚本通过 `--profile` 传入，并在结果标记 `instrumented=true`。实例输入记录这一选项，不能悄悄切换已运行实例的测量模式。固定工具链构建及 4 项补丁检查通过，日志为 `profile-build-v1.log`。

每类请求按 FUSE opcode 和锁类型汇总：第一次执行前的队列等待、服务函数耗时、回复/通知耗时、请求总耗时及重试次数。服务函数包含协议处理和本地锁，不等于纯宿主系统调用耗时。时间从 descriptor 解码后开始，未包含 guest 和此前的传输；取消控制消息不计时。诊断聚合本身增加开销，不能拿这一模式作无插桩性能验收。

服务退出时输出 `HARDDIND_REQUEST_PROFILE_END`。`scripts/analyze-shared-profile.py LOG...` 拒绝缺少结束标记、重复行和不一致计数，再输出平均微秒及原始累计值。默认关闭时不读取时钟或更新诊断计数。真实测量结果另行保留，不由构建成功推断性能改善。
