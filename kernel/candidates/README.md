# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页 | 可应用；此前对照未证明稳定加速 |
| `fault-rendezvous-counters` | 缺页交接计数（诊断用，需叠加在 `anon-faultaround` 之后） | 可应用 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `clock-getcpu-fastpath` | 在 SIGSYS stub 内直接应答 `clock_gettime(MONOTONIC)` 和 `getcpu`，启动参数 `harddind_clock_fastpath=1` | 基于旧的补丁底座，**不能应用到当前 series，需要 rebase** |
| `clock-vdso` | `harddind_vdso_clock=1`：guest TSC clocksource 与通用 vvar/timens 快路径，默认关闭 | 普通容器快路径通过；非零偏移、namespace 切换及调时开/关各 50 项通过；功能 126/130（3 项基线失败及 HTTPS 超时待复核），定向基准待测；coarse/time 未达两倍原生延迟目标（启用后不跟随 host NTP） |
