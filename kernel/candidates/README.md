# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

已移入默认 series：`fault-adaptive`、`fault-populate`、`fault-sorted-alloc`、`fault-hostmap`、`fault-unmap`、`fault-exit`（0040–0045），随后是 `share-profile`、`share-complete`、`share-wait`（0046–0048）。对应缺页与共享延迟开关由 `CORE_ARGS` 启用；请求 profiling 仍默认关闭。`fault-profile` 和 `fault-ram-populate` 保留为默认关闭的候选。

0040–0048 后的应用检查（固定源码、`patch --fuzz=0`；不代表构建或功能通过）：`core-clone-batch`、`core-fp-compare`、`core-fp-lazy`、`core-reply-spin`、`fault-ram-populate`、`gpu-usercopy-lock-order`、`net-profile`、`net-rx-copybreak`、`share-create`、`share-dir-cache`、`share-notify-batch`、`share-sg`、`share-uaccess` 可单独应用；`share-coherent` 需先应用 `share-dir-cache`（profile/complete/wait 已在 series）。其他候选组合未在此检查。

<!-- needs-rebase -->
以下候选目前不能叠加到默认 series，需要 rebase；下表其余构建/运行状态是提升前的历史记录：

| 候选 | 冲突或依赖 |
| --- | --- |
| `core-syscall-patch` | `arch/um/include/asm/mmu.h`、`arch/um/kernel/Makefile`、`arch/um/kernel/tlb.c` |
| `core-syscall-hot` | 依赖尚未 rebase 的 `core-syscall-patch` |
| `fault-rendezvous-counters` | `arch/um/kernel/{Makefile,tlb.c,trap.c}`、`arch/um/os-Linux/skas/mem.c` |
| `core-syscall-profile` | 依赖尚未 rebase 的 `fault-rendezvous-counters`；依赖恢复后需再检查 |
| `fault-profile` | Makefile、TLB、broker mapping 和 stub 的上下文已改变；保持诊断候选，不默认启用 |
| `share-close` | `fs/fuse/virtio_fs.c` 的 include 上下文 |
| `share-poll` | 与已提升的 share-wait 在 `arch/um/drivers/virtio_uml.c` 冲突；原本就禁止两种轮询一起使用 |

`tests/test_kernel_candidates.py` 在构建机缓存了固定 kernel archive 时复查以上清单；普通本机 `./hd check` 没有该 archive 时跳过源码应用检查。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 单独及组合构建通过，probe 已采集计数；计数为全机增量，失败批次不代表命令成功 |
| `core-clone-batch` | `uml_clone_batch=1` 将 runner 栈映射与 clone 放在同一有序队列；组合时放在 `fault-rendezvous-counters` 前 | 默认关闭；构建、队列失败/容量回归、32 项定向功能和语义 A/B 通过；fork/exec 各省 1/2 批次，五组配对速度比中位数 0.94/1.28、范围跨 1；暂不提升 |
| `core-syscall-profile` | 依赖 `fault-rendezvous-counters`；`uml_syscall_profile=1` 约每 256 次采样全部 syscall 分段 TSC，按 vCPU 统计双向交接、两端 spin/futex、空闲周期与 broker batch，另计 syscall 编号、成功 clone/exec 与最终 task exit | 默认关闭；首轮 probe 9 次程序成功、A/B 正常关机；修正计时边界和 EAGAIN 分类后 probe 9 次通过（含默认容器 seccomp、嵌套策略与 ptrace）；未完成完整功能套件；return_gap 含用户指令和调度延迟 |
| `core-reply-spin` | `uml_reply_spin=0..10000` 单独设置 runner 等待内核回复的轮询次数；组合时放在 counters/profile 后 | 默认继承 `seccomp_spin`；已构建，只调整有界等待，不改变交接协议；待 guest 正确性和 A/B 测量 |
| `core-fp-compare` | `uml_fp_compare=1` 恢复 runner 浮点状态前比较完整 xstate；相同才省去复制 | 默认关闭；core-48 原生/A/B 共 9 次通过 SSE/AVX、signal/ptrace 上半部修改、致命 seccomp 与 ptrace 改为 exit；SSE/AVX 配对加速中位数 1.015×/1.054×，范围均跨 1，不足以启用；probe 尚无每样本外层 throttle 记录 |
| `core-syscall-patch` | `uml_syscall_patch=1`：私有文本 COW 后改写固定编号与 Go 寄存器编号 syscall；无 GS 占用，保留常规 guest syscall/ptrace/seccomp 路径 | 默认关闭，不建议推广。远程强制写在 mmap_lock 前恢复站点并持 patch mutex 至写完；单步恢复全部站点并停用改写直到 exec。core-42 全套 155/157，仅 rseq 关闭、share mapped-write 两项既有失败；POKETEXT/proc-mem/单步、glibc/musl/Go/strace、FP、signals/restart 均过；复制栈加载的故障地址 native/A/B 3/3 通过。16 工作者启动 64 容器通过，配额限流 67.0%；快路径占调用 36.6%，动态编号占快路径 82.7%（非唯一站点数）。core-43 寄存器编号 probe 单/四线程配对加速 1.73×/2.09×；core-44/45 quick 矩阵各 6/18 达标，Docker 四项比值下降 27–40%，compile 比值下降 8%，原生样本也明显漂移，不能支持启用。core-37 的 shared-host-mapping panic 与旧 runc ECHILD 根因仍未定，后续重跑未复现。文本自读、PEEKTEXT、proc-mem 可见改写，自校验可能不兼容；每改写文本页一页私有 COW，每 mm 64 KiB RX trampoline（0x06060000）、宿主 4 MiB TID 表及 4 KiB scratch；最多 255 站点，歧义/不支持的指令回退 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `share-poll` | virtio-uml 完成轮询（`virtio_uml.poll_iters=N`），保留中断后备 | 未证明元数据收益，不建议开启 |
| `net-profile` | `uml_net_profile=1`：vector 驱动收发/SIGIO/IRQ 计数和按线程的 CPU 拆分；bench 在网络指标后自动跑带标记的传输诊断（诊断用） | 可应用 |
| `core-syscall-hot` | 依赖 `core-syscall-patch`；`uml_syscall_patch_hot=64` 按 mm/站点延迟改写，首次热站点才分配 trampoline；fork 继承已有站点，exec 前不新增 | 默认 0，保持原候选行为；已构建；core-50 全套 161/163，core/checker/glibc/musl/Go/strace 与两组启动 burst 均过；失败为 outer_mapped_write、outer_directory_permission，后者在 core-51 同内核关闭对照通过，开启时的失败仍未定位；工作负载 A/B 未测，不建议推广 |
| `core-fp-lazy` | `uml_fp_lazy=1` 借用停驻 runner 的完整 FP 映像；切换、信号、ptrace、fork 前复制，同映像返回省去双向复制 | 默认关闭；不改 XSAVE 格式或掩码；生命周期 syscall 仍立即复制；已构建，不可与 core-fp-compare 组合；core-49 原生/A/B 9 次 FP 语义通过，SSE/AVX 配对中位加速 1.417×/1.217×，范围 1.20–1.54/1.05–1.46；无逐样本 throttle 数据；core-52 全套 162/163，仅既有 outer_mapped_write 失败；FP 检查记录 borrowed/reused/materialized=16792/16559/232；真实工作负载待测 |
