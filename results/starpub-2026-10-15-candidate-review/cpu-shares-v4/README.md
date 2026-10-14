# 实际 CPU shares 竞争测试：仍无法判定

Starpub 同一无特权容器、4 vCPU guest、两个各含 4 个工作线程的内部容器，竞争同一个 guest CPU。使用 `cpu-cpuset-kernel-v1`；内核、磁盘、源码和二进制哈希在原始报告中。没有修改外层权限或性能验收标准。

| 运行 | 实际结果 | 原因 |
| --- | --- | --- |
| [v3](../cpu-shares-v3/runtime/shares-v3/report.json) | fail | `json_share_cg` 多输出一个 `}`，实际 contend 的首尾两行不是合法 JSON；中间采样行有效 |
| [v4](runtime/shares-v4/report.json) | indeterminate | 修复首尾序列化后，反转权重阶段仍有采样空档，等权阶段的共同有效窗口不足 |

v4 只修复该序列化字符，并补充真实二进制 100 ms contend 回归。相同原版二进制的首尾行复现失败，修复版全部记录能解析，见 `endpoint-regression.json` 和 `original-contend.stdout.gz` / `fixed-contend.stdout.gz`。包含文件/FIFO 捕获和真实首尾序列化的 59 项测试通过；v3 的 58 项测试未覆盖这个实际入口。

实际权重为 100/59：固定 Moby 版本把 shares 1024/512 转换为这两个值。第一阶段观察到 A 的 CPU 份额区间为 0.628649–0.628946，对应权重期望值 0.628931。这个阶段通过功能分配检查。反转阶段有约 0.76–1.89 秒的采样空档；等权阶段只有约 3.31 秒的共同有效窗口，小于现有 4 秒要求。两个阶段均不能判为通过，阶段顺序检查也没有完成。

17 项检查为 14 pass、2 indeterminate、1 not_tested。线程驻留、累计计量、实际更新、输入稳定和正常关机通过；独立检查确认所有 51 个记录进程组均为空。第一阶段的分配区间及判定容差只用于资源控制功能，不是用户认可的性能阈值，也不能代表整体效率。

运行器没有把用于判定的 `ev` 保存到 `report.evidence`，该字段为空。原始每条命令输出、时间和源码均保留，但本归档尚不声称能完整重放所有判定；后续必须修复证据保存和实际阶段时序，继续诊断采样停顿。`preflight-attempt1/` 保留了启动实测前读错 POSIX 结果字段的调度失败；该次没有启动 guest 或执行测试命令。

从仓库根目录运行 `python3 results/starpub-2026-10-15-candidate-review/recheck-runtime-candidates.py` 可验证归档哈希、源码绑定、真实序列化差异、判定边界和进程组清理。
