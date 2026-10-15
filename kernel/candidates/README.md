# 内核候选

`./hd ... --with NAME` 在 `kernel/series` 之后按给出的顺序应用 `NAME.patch`。证明有效后移入 `kernel/patches/` 并加进 series。

| 候选 | 内容 | 状态 |
| --- | --- | --- |
| `anon-faultaround` | 匿名缺页时批量映射相邻页 | 可应用；此前对照未证明稳定加速 |
| `fault-rendezvous-counters` | 缺页交接计数（诊断用，需叠加在 `anon-faultaround` 之后） | 可应用 |
| `gpu-usercopy-lock-order` | GPU ioctl 与 mmap 锁顺序修复 | 可应用；多上下文仍有失败 |
