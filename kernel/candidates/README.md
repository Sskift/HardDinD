# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页，`uml_faultaround=5` 为 32 页窗口 | 32 项定向功能通过；首触交接降到每 32 页一次；当前 memfd、关闭计数器、与 edges 组合的五对反向 probe 的配对速度比为 2.23（范围 1.98–3.57）、原生比例 0.42；quick mem.touch 为 0.228，未达标 |
| `fault-rendezvous-counters` | 独立诊断候选，`uml_faultstat=1`；区分合并前映射请求、合并后提交命令、非空批次、runner 缺页/系统调用和内核侧 futex 等待；无时钟探针 | 单独及组合构建通过，probe 已采集计数；计数为全机增量，失败批次不代表命令成功 |
| `core-set-ptes` | 修复批量 `set_ptes()` 按剩余数量跳跃 PFN 的错误，每个 PTE 只前进一页；纯正确性修复，无启动开关 | 原函数三页失败，修复后 1–512 页回归通过；32 项定向功能通过；当前内核 native/A/B 各两次自有语义通过；建议独立提升，完整候选 CI 未测 |
| `core-faultaround-edges` | 依赖 `anon-faultaround`；`uml_faultaround_edges=1` 将窗口裁剪到 VMA 边界，允许不足完整窗口的小型匿名 VMA 预取；保留原有排除条件和同步失效规则 | 默认关闭；构建、窗口回归、32 项定向功能和语义 A/B 通过；16 页小 VMA 缺页 16→1、批次 18→3；单独增量五对速度比 1.56（0.86–1.92），当前关闭计数器的组合收益 2.14（0.61–2.59）；耗时仍不稳定 |
| `core-clone-batch` | `uml_clone_batch=1` 将 runner 栈映射与 clone 放在同一有序队列；组合时放在 `fault-rendezvous-counters` 前 | 默认关闭；构建、队列失败/容量回归、32 项定向功能和语义 A/B 通过；fork/exec 各省 1/2 批次，五组配对速度比中位数 0.94/1.28、范围跨 1；暂不提升 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `net-fd-vnet` | fd 网络传输的 vnet 头：GSO、校验和卸载；流水线检测到该候选时打开后端 vnet 和批量 16 | 出站 0.04→0.28，发布 0.08→0.12；HTTPS 偶发超时未查清 |
| `share-poll` | virtio-uml 完成轮询（`virtio_uml.poll_iters=N`），保留中断后备 | 未证明元数据收益，不建议开启 |
| `share-profile` | FUSE 每种请求的往返计时与 tracepoint（诊断用） | 可应用 |
