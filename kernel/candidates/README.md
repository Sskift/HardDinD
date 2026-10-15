# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页，`uml_faultaround=5` 为 32 页窗口 | 32 项定向功能通过；首触交接降到每 32 页一次；当前 memfd、关闭计数器、与 edges 组合的五对反向 probe 的配对速度比为 2.23（范围 1.98–3.57）、原生比例 0.42；quick mem.touch 为 0.228，未达标 |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 单独及组合构建通过，probe 已采集计数；计数为全机增量，失败批次不代表命令成功 |
| `core-faultaround-edges` | 依赖 `anon-faultaround`；`uml_faultaround_edges=1` 将窗口裁剪到 VMA 边界，允许不足完整窗口的小型匿名 VMA 预取；保留原有排除条件和同步失效规则 | 默认关闭；构建、窗口回归、32 项定向功能和语义 A/B 通过；16 页小 VMA 缺页 16→1、批次 18→3；单独增量五对速度比 1.56（0.86–1.92），当前关闭计数器的组合收益 2.14（0.61–2.59）；耗时仍不稳定 |
| `core-clone-batch` | `uml_clone_batch=1` 将 runner 栈映射与 clone 放在同一有序队列；组合时放在 `fault-rendezvous-counters` 前 | 默认关闭；构建、队列失败/容量回归、32 项定向功能和语义 A/B 通过；fork/exec 各省 1/2 批次，五组配对速度比中位数 0.94/1.28、范围跨 1；暂不提升 |
| `core-syscall-profile` | 依赖 `fault-rendezvous-counters`；`uml_syscall_profile=1` 每 256 次采样 syscall 分段 TSC，按 vCPU 统计双向交接和两端 spin/futex | 默认关闭；首轮 probe 9 次程序成功、A/B 正常关机；修正计时边界和 EAGAIN 分类后 probe 9 次通过（含默认容器 seccomp、嵌套策略与 ptrace）；未完成完整功能套件；return_gap 含用户指令和调度延迟 |
| `core-reply-spin` | `uml_reply_spin=0..10000` 单独设置 runner 等待内核回复的轮询次数；组合时放在 counters/profile 后 | 默认继承 `seccomp_spin`；已构建，只调整有界等待，不改变交接协议；待 guest 正确性和 A/B 测量 |
| `core-fp-compare` | `uml_fp_compare=1` 恢复 runner 浮点状态前比较完整 xstate；相同才省去复制 | 默认关闭；原生 SSE/AVX 线程切换及信号/ptrace 修改 YMM 上半部通过；不省略入口保存、不使用脏标记；guest 和性能待测 |
| `core-syscall-patch` | `uml_syscall_patch=1`：私有文本 COW 后改写 7/9 字节 syscall；无 GS 占用，TID 查找 runner 后直接交接/恢复；非普通返回走完整 sigreturn | 默认关闭、独立候选，不应提升；旧版 core-22 为 141/144（3 项已有 share 失败），core-24 为 41/145，runc 子任务启动 runner 时 ECHILD 导致 panic；根因未定。修正 host stub 过滤器分支并增加退出状态诊断后复测中。旧版静态 glibc/musl、Go、strace -f 及改写/FP/生命周期检查通过，指令单步未验证；core-23 的 getpid 1/4 线程速度比 1.83/2.01 仅作机制诊断，策略/ptrace 热点未改写，现已增强检查。保守解码拒绝前缀/歧义序列；mremap、trampoline 映射变更及文本变为可写前恢复原指令。文本自读、PEEKTEXT、/proc/pid/mem 可见改写，自校验/代码签名程序可能不兼容；每个改写文本页多一页私有匿名内存，另有每 mm 64 KiB RX trampoline VMA 和宿主 4 MiB 稀疏 TID 表；fork 继承，exec 丢弃；最多 511 站点，耗尽后回退 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `share-poll` | virtio-uml 完成轮询（`virtio_uml.poll_iters=N`），保留中断后备 | 未证明元数据收益，不建议开启 |
| `share-profile` | FUSE 每种请求的往返计时与 tracepoint（诊断用） | 可应用 |
