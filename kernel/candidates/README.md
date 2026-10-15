# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页，`uml_faultaround=5` 为 32 页窗口 | 32 项定向功能通过；文件 RAM 后端五组 probe 的配对速度比中位数为 3.51、原生比例 0.46；首触交接降到每 32 页一次，当前 memfd 默认配置待重测 |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 单独及组合构建通过，probe 已采集计数；计数为全机增量，失败批次不代表命令成功 |
| `core-set-ptes` | 修复批量 `set_ptes()` 按剩余数量跳跃 PFN 的错误，每个 PTE 只前进一页；纯正确性修复，无启动开关 | 原函数三页失败，修复后 1–512 页回归通过；32 项定向功能和自有容器语义通过；完整功能仍未测 |
| `core-faultaround-edges` | 依赖 `anon-faultaround`；`uml_faultaround_edges=1` 将窗口裁剪到 VMA 边界，允许不足完整窗口的小型匿名 VMA 预取；保留原有排除条件和同步失效规则 | 默认关闭；构建、窗口回归、32 项定向功能和语义 A/B 通过；16 页小 VMA 缺页 16→1、批次 18→3；五组配对速度比中位数 1.56、范围 0.86–1.92，待空闲 CPU 复测 |
| `core-clone-batch` | `uml_clone_batch=1` 将 runner 栈映射与 clone 放在同一有序队列；组合时放在 `fault-rendezvous-counters` 前 | 默认关闭；构建、队列失败/容量回归、32 项定向功能和语义 A/B 通过；fork/exec 各省 1/2 批次，五组配对速度比中位数 0.94/1.28、范围跨 1；暂不提升 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `clock-getcpu-fastpath` | 在 SIGSYS stub 内直接应答 `clock_gettime(MONOTONIC)` 和 `getcpu`，启动参数 `harddind_clock_fastpath=1` | 基于旧的补丁底座，**不能应用到当前 series，需要 rebase** |
| `clock-vdso` | `harddind_vdso_clock=1`：guest TSC clocksource 与只读 vvar，默认 syscall；time namespace 回退 | 编译通过；初始 time namespace 开/关各 50 项通过，常规容器因非初始 time namespace 回退；coarse/time 未达两倍原生延迟目标，完整评分未测（启用后不跟随 host NTP） |
