# POSIX 范围修复候选：失败与独立复测

范围修复候选在第二次独立运行中完成了 **原生 38/38、内部 Docker 38/38 的现有功能检查**，正常关机，103 个记录的进程组最终均为空。首次运行的启动与关机失败完整保留。候选仍没有合入正式补丁目录，现有矩阵也不代表完整 POSIX 语义验收。

| 实例 | 原生 | 内部 Docker | 生命周期 |
| --- | --- | --- | --- |
| `posix-range-v2` | 38/38 通过 | 38 项缺测 | API 就绪前超时，正常关机超时，最终 SIGKILL |
| `posix-range-v2b` | 38/38 通过 | 38/38 通过 | 正常关机，guest、网络、文件系统和探针进程组无残留 |

第二次使用相同的内核、磁盘、virtiofsd、探针及测试脚本哈希，换用新实例名，提前等待全局构建锁，再启动功能测试。`status-retry.json` 分别记录了排队、取得锁、启动测试和结束时点；不能把包括排队的总时间当性能数据，也不能仅凭重试成功认定首次启动超时的原因已解决。

相对 [上一版失败](../starpub-2026-10-15-posix-candidate-v1/README.md)，候选修正了 FUSE 包含式端点 `(0,0)` 和有符号 64 位最大偏移。除了原有单字节、EOF 用例，本轮确实执行了高于 `2^62` 的范围、`INT64_MAX` 单字节和高偏移 EOF 的检查。第二次运行中，这些检查在原生及内部 Docker 均通过。

候选作者的脚本遗漏了大偏移用例的执行列表，而且在保存 JSON 后才设置 `functional_complete`。主代理在冻结的测试副本中修正后运行；原脚本保存在 `candidate-build/run-shared-locks-agent.py`，修正记录在 `owner-harness-fixes.json`，实际脚本在 `repo/scripts/run-shared-locks.py`。PID 归属、死锁检测、exec 与权限生命周期仍有未完成项，见作者的 `candidate-build/REPORT-v2.md`；该报告中的作者验证不等同于主代理验收。

特别说明两个现有用例的边界：`posix_getlk_reports` 在 Docker 侧只检查 PID 非负，尚不能证明持有者 PID 正确；`posix_whence_cur_end` 的 end 分支先做 `lseek(SEEK_END)`，实际加锁仍使用 `SEEK_CUR`，不能声称已经直接验证 `fcntl` 的 `SEEK_END`。这两项已反馈给后续修复，保留原始测试和名称，不事后扩大通过范围。

## 实际故障与证据

以下故障记录对应第一次运行；第二次原始结果位于 `run/results/posix-range-v2b.json`，完整 76 条原生及 Docker 检查状态位于 `runtime-retry.log.gz`。`evidence-integrity-v2b.json` 的 26 项核对通过，`cleanup-audit-v2b.json` 保存 103 个进程组的独立扫描；它们不替换首次故障记录。

- guest 日志确认共享目录已挂载；端口驱动没有收到 daemon 的连接，Docker API 没有就绪。
- 原始命令日志记录 `startup timed out` 与 `supervisor did not finish shutdown`。这还不能判定为锁补丁本身造成的启动故障。
- 测试报告保存时 supervisor 仍在 `stopping`；实例最终状态为 `failed`，`forced_shutdown=true`，guest 退出码为 `-9`。两个时点分别原样保存在 `run/results/posix-range-v2.json` 和 `run/instances/posix-range-v2/state.json`，没有用后来的结果改写早先报告。
- 50 个原生探针进程组，加上 guest、网络和文件系统服务，共 53 个记录的进程组在最终独立扫描时均为空。没有残留不代表正常关机成功。
- `evidence-integrity.json` 的 25 项核对通过，检查范围是原始结果、输入哈希和构建身份。`--functional-only` 没有采样性能，`benchmark_complete=false`。

这次故障还暴露了主线报告问题：内联锁检查把先前的启动错误覆盖成 `missing inline execution evidence`。主线已改为保留最早的 `error`，并另存 `inline_error`，不改变失败判定。`owner-validation/` 保留该修改在 Starpub 上的 39 项测试通过记录；最初上传检查副本时漏传测试 fixture 的失败也一并保留，随后补齐输入并重跑。历史运行报告不作事后修改。

## 固定输入

- 内核：`9192f196143242aab3dfacc2966d7bcc605db9cc0caadfe68b09a76cb42b557e`
- 候选 virtiofsd：`04ead9768a8ab98462f5726cc55544ce60f7221723482148ea41f913f480cfdd`
- 候选 `0005`：`0886ea7b1403af66af119c8db67569f4814b4b6e164a0752e21bc840865e77ce`
- 实际测试脚本：`3748395d821f360999e9746edf003184e24107a72984ff862a72703f89902a21`

完整启动命令见 `status.json`。后续对照使用新的实例名与日志，保留本次失败，不覆盖它。
