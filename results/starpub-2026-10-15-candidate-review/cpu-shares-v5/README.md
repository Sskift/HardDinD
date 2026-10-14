# CPU shares v5：执行后报告生成失败

本轮原生检查 100 项通过，随后在 Starpub 执行真实 Docker shares 更新、观察、SIGTERM 收尾和清理。guest 最终正常关闭，但运行器在生成报告时抛出 `AttributeError: 'str' object has no attribute 'get'`，没有保存 `report.json`。本轮判定失败，不能从原始负载输出补称功能通过。

直接原因是 `run-cpu-shares.py` 对每条命令输出逐行调用 `json.loads`，随后假定所有结果均为对象。实际 `instance-start.stdout` 的格式化 JSON 内含独立的字符串行，`docker wait` 则输出整数 `0`；二者都是合法 JSON 值，却没有 `.get()` 方法。100 项检查未覆盖真实报告收尾路径。

冻结源码、所有原始 stdout/stderr、命令文本日志、作者报告及之前 v4 的离线重算均保留。主代理在进程结束后补做 `owner-postfailure-audit.json`：源码哈希未变，guest 与网络后端正常停止，四个可恢复的进程组为空。完整命令 PID/清理元数据尚在进程内存中时就发生异常，已无法完整恢复，所以没有声称所有命令进程组均经审计。

后续修复需同时完成输出类型识别和每条命令结束后立即持久化证据，并用这轮实际输出覆盖真实报告生成路径。修复前的原始失败不覆盖；新一轮实测仍须重新取得完整证据。性能和完整功能均未验收。
