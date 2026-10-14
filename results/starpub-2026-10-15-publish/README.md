# IPv6 与外层地址发布候选：未通过

本轮仍保留上游 Moby，只测试额外的端口驱动补丁。所有构建和实际 Docker 命令均在 Starpub 的 `/root/harddind-rebuild/publish-owner-v3` 执行；没有修改 `/root/udock`、外层权限或宿主网络配置。候选实现尚未合入正式源码。

## 完整候选用例 v3

[原始报告](v3/runtime/publish-v3.json) 为 **11 pass、1 fail、9 not_tested**，`subset_passed`、`full_acceptance` 和 `performance_acceptance` 均为 false。

- 已通过：daemon 接入、实际 guest 地址的引用计数和两轮释放复用、启动失败后的地址回滚、固定 IPv4 TCP/UDP 数据校验、停止重启及删除释放、默认和自定义 bridge 的端口冲突回滚、多端口原子回滚、正常关闭。
- 失败：IPv6 外层地址发布。TCP 和 UDP 容器均创建成功，后续回显超时。本次原始用例没有记录每个报文的阶段，因此仅凭该报告不能区分 TCP、UDP 或具体报文大小。
- 未测：后续不同外层地址共用端口、通配地址冲突、手工映射所有权、临时端口、范围、`-P`、并行发布和实例重启恢复。此前版本的通过结果不能代替这些候选上的缺测。

实际地址由 `network-probe addresses vec0` 读取。TCP 与 UDP 共用 `127.0.0.2` 时，guest 只增加一个 `10.0.2.17`；删掉 TCP 后保留，删掉 UDP 后释放，第二轮复用同一地址。占用外层端口导致容器启动失败时，guest 地址集合保持不变。此次 IPv6 是上游 Docker proxy 将 IPv6 主机监听转给 IPv4 bridge 容器，不包含 IPv6 容器网络的完整验收。

构建固定 Moby 提交 `8af9fe3a36bab3e039862a2ab1cef1880c9b4d03`，实际 daemon 版本为 `29.8.2-harddind.1`，候选 `0003` 补丁 SHA256 为 `5a76bcdc9ecdf2f4ac8dc81daf15ca0833cd9a9c6081759142af68ebc3e08417`。内核为 cpuset 修复版，SHA256 `fb5e56e48240f96a997aa0f1d234992f128d23dd428eb8256dfccccc4d217fca`；具体输入及测试前后哈希见报告。

运行前的 7 项端口驱动单元测试、11 项原生 backend 检查，以及两个 Moby Go 包测试均通过。它们没有发现后来的实际 IPv6 超时，不能替代内部 Docker 验收。Moby 使用 `JOBS=4` 完整编译；非法 JOBS 的拒绝、脚本语法和静态地址探针编译均已检查。

最终正常关机，guest/network 退出码均为 0，没有强制关闭，自动端口列表为空。56 个命令进程组及 guest/network 两个进程组均独立复查为空；全部记录输入哈希保持一致。[证据审计](v3/evidence-audit.json) 的 62 项是失败记录的完整性检查，不是 62 项功能通过。

`v3/sources/` 保存本次实际使用的候选与主代理测试器，`v3/validation/` 保存原生检查输出，`v3/runtime/` 保存逐条命令、实例及运行日志，`v3/owner/` 保存构建、监督器和独立清理审计。构建监督器统一持有排他锁，因此这份实验副本的 network/port-agent 构建脚本去掉了内层重复加锁；主线对应脚本没有更改。作者报告只作交付记录，结论以上述实际运行证据为准。

## 小报文诊断 v1

随后使用同一内核、磁盘和端口补丁，单独编译加入有限 IPv6 报头跟踪的 backend，在新实例 `publish-diag1` 运行。[诊断报告](diagnostic-v1/report.json) 显示：外层 IPv4 TCP、IPv6 TCP、IPv6 UDP 的 25 字节报文全部正确往返；guest 内直接连接 IPv6 proxy 地址也正确回显，IPv6 网关 ping 成功。

这确认了该次运行的地址、邻居解析和基本代理链路可用，但没有重现大报文检查，不能据此覆盖 v3 的失败。后续将分别检查报文大小与方向。该诊断正常关闭，16 个记录的进程组独立复查为空，输入哈希保持一致；完整功能和性能验收字段仍为 false。完整源码、命令及报头日志保存在 `diagnostic-v1/`。

另在候选源码审查中发现控制接口 `list` 的固定输出缓冲区与增大的端口表上限不匹配，连续 `snprintf` 未检查截断后的位置，存在越界写风险。修复及大端口表检查尚在进行；该候选不能合入作为已可用实现。
