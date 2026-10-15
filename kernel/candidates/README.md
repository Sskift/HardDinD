# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页 | 可应用；此前对照未证明稳定加速 |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 与 `anon-faultaround`、`core-set-ptes` 组合构建通过；计数为全机增量，失败批次不代表命令成功；容器内验证待套件接入 |
| `core-set-ptes` | 修复批量 `set_ptes()` 按剩余数量跳跃 PFN 的错误，每个 PTE 只前进一页；纯正确性修复，无启动开关 | 组合构建通过；实际函数回归覆盖 1–512 页连续 PFN、标志位和同步范围，原实现三页即失败；容器内文件映射验证待套件接入 |
| `core-faultaround-edges` | 依赖 `anon-faultaround`；`uml_faultaround_edges=1` 将窗口裁剪到 VMA 边界，允许不足完整窗口的小型匿名 VMA 预取；保留原有排除条件和同步失效规则 | 默认关闭；窗口几何回归通过；内核与容器语义验证中 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `clock-getcpu-fastpath` | 在 SIGSYS stub 内直接应答 `clock_gettime(MONOTONIC)` 和 `getcpu`，启动参数 `harddind_clock_fastpath=1` | 基于旧的补丁底座，**不能应用到当前 series，需要 rebase** |
| `clock-vdso` | `harddind_vdso_clock=1`：guest TSC clocksource 与只读 vvar，默认 syscall；time namespace 回退 | 编译通过；初始 time namespace 开/关各 50 项通过，常规容器因非初始 time namespace 回退；coarse/time 未达两倍原生延迟目标，完整评分未测（启用后不跟随 host NTP） |
