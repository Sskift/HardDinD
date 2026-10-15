# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页 | 可应用；此前对照未证明稳定加速 |
| `fault-rendezvous-counters` | 缺页交接计数（诊断用，需叠加在 `anon-faultaround` 之后） | 可应用 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
| `net-fd-vnet` | fd 网络传输的 vnet 头：GSO、校验和卸载；流水线检测到该候选时打开后端 vnet 和批量 16 | 出站 0.04→0.28，发布 0.08→0.12；HTTPS 偶发超时未查清 |
| `share-poll` | virtio-uml 完成轮询（`virtio_uml.poll_iters=N`），保留中断后备 | 未证明元数据收益，不建议开启 |
| `share-profile` | FUSE 每种请求的往返计时与 tracepoint（诊断用） | 可应用 |
