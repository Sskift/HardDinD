# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 单独及组合构建通过，probe 已采集计数；计数为全机增量，失败批次不代表命令成功 |
| `core-clone-batch` | `uml_clone_batch=1` 将 runner 栈映射与 clone 放在同一有序队列；组合时放在 `fault-rendezvous-counters` 前 | 默认关闭；构建、队列失败/容量回归、32 项定向功能和语义 A/B 通过；fork/exec 各省 1/2 批次，五组配对速度比中位数 0.94/1.28、范围跨 1；暂不提升 |
| `core-syscall-profile` | 依赖 `fault-rendezvous-counters`；`uml_syscall_profile=1` 每 256 次采样 syscall 分段 TSC，按 vCPU 统计双向交接和两端 spin/futex | 默认关闭；首轮 probe 9 次程序成功、A/B 正常关机；修正计时边界和 EAGAIN 分类后 probe 9 次通过（含默认容器 seccomp、嵌套策略与 ptrace）；未完成完整功能套件；return_gap 含用户指令和调度延迟 |
| `core-reply-spin` | `uml_reply_spin=0..10000` 单独设置 runner 等待内核回复的轮询次数；组合时放在 counters/profile 后 | 默认继承 `seccomp_spin`；已构建，只调整有界等待，不改变交接协议；待 guest 正确性和 A/B 测量 |
| `core-fp-compare` | `uml_fp_compare=1` 恢复 runner 浮点状态前比较完整 xstate；相同才省去复制 | 默认关闭；原生 SSE/AVX 线程切换及信号/ptrace 修改 YMM 上半部通过；不省略入口保存、不使用脏标记；guest 和性能待测 |
| `core-syscall-patch` | `uml_syscall_patch=1`：私有文本 COW 后改写固定编号与 Go 寄存器编号 syscall；无 GS 占用，保留常规 guest syscall/ptrace/seccomp 路径 | 默认关闭，不建议推广。远程强制写在 mmap_lock 前恢复站点并持 patch mutex 至写完；单步恢复全部站点并停用改写直到 exec。core-42 全套 155/157，仅 rseq 关闭、share mapped-write 两项既有失败；POKETEXT/proc-mem/单步、glibc/musl/Go/strace、FP、signals/restart 均过；复制栈加载的故障地址 native/A/B 3/3 通过。16 工作者启动 64 容器通过，配额限流 67.0%；快路径占调用 36.6%，动态编号占快路径 82.7%（非唯一站点数）。core-43 寄存器编号 probe 单/四线程配对加速 1.73×/2.09×；core-44/45 quick 矩阵各 6/18 达标，Docker 四项比值下降 27–40%，compile 比值下降 8%，原生样本也明显漂移，不能支持启用。core-37 的 shared-host-mapping panic 与旧 runc ECHILD 根因仍未定，后续重跑未复现。文本自读、PEEKTEXT、proc-mem 可见改写，自校验可能不兼容；每改写文本页一页私有 COW，每 mm 64 KiB RX trampoline（0x06060000）、宿主 4 MiB TID 表及 4 KiB scratch；最多 255 站点，歧义/不支持的指令回退 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `share-poll` | virtio-uml 完成轮询（`virtio_uml.poll_iters=N`），保留中断后备 | 未证明元数据收益，不建议开启 |
| `share-profile` | FUSE 每种请求的往返计时与 tracepoint（诊断用） | 可应用 |
| `net-profile` | `uml_net_profile=1`：vector 驱动收发/SIGIO/IRQ 计数和按线程的 CPU 拆分；bench 在网络指标后自动跑带标记的传输诊断（诊断用） | 可应用 |
