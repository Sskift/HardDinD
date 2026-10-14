# Starpub 共享地址空间并行执行实验

当前仍未达到“完整 Docker、原生相同性能、GPU”目标。本轮实际修改 Linux UML 执行核心，使同一地址空间的线程能够并行运行；未修改宿主内核，也未实现 GPU 后端。

最终核验的内核 SHA256 为 `a984d8f29a9181885d4057fec03e9f0d014a67106922706a3462de3bb0f19e50`。对应默认补丁 0001 加实验补丁 0003，未叠加交接协议实验 0002。上游、配置、编译器、补丁哈希和外层权限状态分别保存在 `threads-build.json`、`threads-kernel.config`、`threads-applied-patches.json`。补丁从实验切回默认、再恢复实验后，所有源码文件哈希一致，见 `threads-patch-roundtrip.json`。

## 最终性能对照

见 `threads-comparison.json` 和 `threads-ab-*`。原生和两条 guest 路径均固定在宿主 CPU 0、1、2、3；guest 使用 4 个虚拟 CPU、2 GiB 内存、`seccomp_spin=256`。顺序为原生、旧路径、并行、并行、旧路径、原生；每类每项 6 次测量，表中为中位数，单位秒。同一份静态基准的 SHA256 为 `f43b2ece66091df375b78c1f03cf91367c8977c700280f64c8f4ee1b4438a73e`。

| 项目 | 原生 | 旧路径 | 并行实验 |
|---|---:|---:|---:|
| 100,000 次 getpid 系统调用 | 0.13096 | 0.96585 | 1.02769 |
| 100 次 fork/exec | 0.06691 | 1.05231 | 1.31534 |
| 单线程计算 | 0.39188 | 0.42655 | 0.43981 |
| 四线程计算 | 0.44861 | 2.58424 | 0.53265 |
| 单进程计算 | 0.39398 | 0.44271 | 0.44280 |
| 四进程计算 | 0.42680 | 0.45395 | 0.47413 |

四线程相对旧路径加速约 4.85 倍，但这一轮仍比原生慢约 18.7%。系统调用和 fork/exec 仍约为原生的 7.85 倍和 19.66 倍。CPU 没有独占保留，多轮存在明显波动；不能从单轮相近或更快认定性能一致。没有设定容忍阈值，没有测完存储、网络和 GPU 工作负载，整体性能验收仍失败。

## 正确性与 Docker

`threads-reviewed-semantics-1` 和 `threads-reviewed-semantics-2` 为最终内核上的两次独立 COW 启动，均通过：

- 四线程共享内存、TLS、互斥锁、barrier/futex 和跨虚拟 CPU 迁移。
- 200 轮写权限撤销、解除映射和固定地址重映射，其他 CPU 必须观察到 SIGSEGV 或新内容。
- 另一线程持续写入时执行 40 次 fork，子进程的 COW 副本必须保持稳定。
- 100 次创建与回收线程。
- 四线程分别对独立、2 MiB 对齐的区域进行 200 轮映射、权限调整和销毁，以触发完整页表的并发创建和释放。

最终语义程序 SHA256 为 `17cb6ffbeeaaece0420d3bc5e8a94ff5926abfa3481e310b1db0e5afe0eb05b3`，同一二进制在外层原生和内层 Docker 中都通过。`threads-reviewed-ptrace` 的 18 项微基准也完成，验证备用执行路径仍能启动和执行。

`docker-threads-reviewed.json` 和原始日志记录原版 Docker 29.8.2、overlayfs、32 项检查：**29 pass、1 fail、2 not_tested**。新增的容器内四线程计算和上述全部线程语义通过，已有的 BuildKit、bridge/DNS、IPv6、guest 内端口发布、卷、bind mount、OOM、pids、生命周期等检查保持通过。GPU 在 `--gpus all` 接入阶段失败；对外网络及完整上游兼容性缺测。因此整个套件和项目验收仍失败。

`threads-original-cow` 使用未应用本次实验的旧内核，`threads-legacy-cow-failure` 使用中间版本关闭并行模式；两者均在并发 fork/COW 检查失败。失败记录保留，没有用纯性能微基准的成功覆盖它们。它们使用较早的语义程序版本，COW 检查与最终版本相同，最终版本另加强了 TLS 读取并增加页表销毁测试。

最终日志被重新解析并核对内核、补丁及测试程序哈希，见 `evidence-validation.json`。清理后未发现实验执行进程或临时 COW 文件；原 `/root/udock` 仍返回 Docker 29.8.2、0 个运行容器、6 个镜像，见 `threads-cleanup.json`。

## 开发过程中的记录

`threads-c4-evidence/` 是页表快照和关闭清理最终审查之前的一轮对照，四线程为 0.398 秒，原生为 0.436 秒。它不替代上面的最终版本结果。

`threads-build-noise-evidence/` 是与后续编译工作发生重叠的一轮对照，保留原始数据但排除出最终性能结论。构建和运行脚本随后增加了共享／独占锁；运行器同时校验运行前后内核哈希。一次更早的内核变化被对照脚本拒绝，没有生成有效汇总。

其他 `initial`、`final`、`verified`、`teardown` 文件名是开发过程中的实验名；本页的最终核验只引用 `reviewed` 文件和顶层 `threads-comparison.json`，以哈希为准。

## 实验边界

并行执行、映射同步和正常生命周期已得到上述实测支持。异常退出、资源耗尽、长期运行及 stub 区域的内部隔离仍未完成充分验证，部分失败路径会终止实验 guest。当前补丁保持显式 opt-in，不作为生产默认后端。GPU ABI、UVM、设备 mmap、锁页内存以及内层 CUDA 生命周期仍需要实现，不能用外层 GPU 成功代替。
