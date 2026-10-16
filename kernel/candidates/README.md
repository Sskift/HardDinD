# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 单独及组合构建通过，probe 已采集计数；计数为全机增量，失败批次不代表命令成功 |
| `core-clone-batch` | `uml_clone_batch=1` 将 runner 栈映射与 clone 放在同一有序队列；组合时放在 `fault-rendezvous-counters` 前 | 默认关闭；构建、队列失败/容量回归、32 项定向功能和语义 A/B 通过；fork/exec 各省 1/2 批次，五组配对速度比中位数 0.94/1.28、范围跨 1；暂不提升 |
| `core-syscall-profile` | 依赖 `fault-rendezvous-counters`；`uml_syscall_profile=1` 每 256 次采样 syscall 分段 TSC，按 vCPU 统计双向交接和两端 spin/futex | 默认关闭；首轮 probe 9 次程序成功、A/B 正常关机；修正计时边界和 EAGAIN 分类后 probe 9 次通过（含默认容器 seccomp、嵌套策略与 ptrace）；未完成完整功能套件；return_gap 含用户指令和调度延迟 |
| `core-reply-spin` | `uml_reply_spin=0..10000` 单独设置 runner 等待内核回复的轮询次数；组合时放在 counters/profile 后 | 默认继承 `seccomp_spin`；已构建，只调整有界等待，不改变交接协议；待 guest 正确性和 A/B 测量 |
| `core-fp-compare` | `uml_fp_compare=1` 恢复 runner 浮点状态前比较完整 xstate；相同才省去复制 | 默认关闭；原生 SSE/AVX 线程切换及信号/ptrace 修改 YMM 上半部通过；不省略入口保存、不使用脏标记；guest 和性能待测 |
| `core-syscall-patch` | `uml_syscall_patch=1`：私有文本 COW 后改写固定编号与 Go 寄存器编号 syscall；无 GS 占用，保留常规 guest syscall/ptrace/seccomp 路径 | 默认关闭。Round 5 强制写/单步的 22 项定向 guest 检查通过（其余 132 项未测）；动态入口内核已构建，全套功能待测。强制远程写在 mmap_lock 前恢复站点并持 patch mutex 至写完；单步前恢复该 mm 的全部站点，直到 exec 不再改写。Go 的多个 MOV 边界保留无效指令，恢复余下寄存器搬运后进入 syscall；动态 RAX 用单次抢锁保存，争用及 fork/exec/exit 等走 SIGSYS。原生发射器测试覆盖参数、flags、红区、SSE/AVX 和争用回退；新 Go 检查要求真实 runtime wrapper 已改写。新增 32 次 Docker 生命周期配合 30 秒宿主配额压力检查及快/慢路径计数。ECHILD 增加活性确认、EINTR 重试和启动分段日志；旧 runc panic 根因未定。Round 4 全套 141/144（3 项 share 失败），旧 getpid 1/4 线程 A/B 1.48/1.58、负载 35–112，不代替本轮验收。文本自读、PEEKTEXT、/proc/pid/mem 可见改写，自校验程序可能不兼容；每改写文本页一页私有 COW，每 mm 64 KiB RX trampoline（0x06060000）及宿主 4 MiB TID 表加 4 KiB scratch；255 站点，耗尽/歧义/不支持的指令仍回退 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `share-poll` | virtio-uml 完成轮询（`virtio_uml.poll_iters=N`），保留中断后备 | 未证明元数据收益，不建议开启 |
| `share-profile` | FUSE 每种请求的往返计时与 tracepoint（诊断用） | 可应用 |
