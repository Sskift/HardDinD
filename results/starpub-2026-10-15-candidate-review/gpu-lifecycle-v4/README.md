# CUDA 生命周期原生对照 v4：失败

本轮只验证 Starpub 原生四卡探针，未执行内部 Docker GPU 验收。原始 [owner-report.json](owner-report.json) 和 [native-verdict.json](native-verdict.json) 保留失败。原生进程以 SIGPIPE（-13）退出，子进程结果及最终 summary 缺失，不能判为通过。

本轮修复了 v3 的子进程记录换行解析、每轮计算时间戳和 setup 后同步问题。owner 另发现 `current_match` 输出为数字，而检查器要求布尔值；已修复实际序列化，`original-shared-json.stdout.gz` 和 `shared-json.stdout.gz` 保留真实 C 发射函数的修复前后证据。还消除了负面 fixture 的重叠 snprintf。57 项检查器测试和 ASan/UBSan fixture 通过。

实际原生运行又暴露三处探针错误：

- 四个共享 context 工作线程的计算、传输和结果检查完成，但清除当前 context 后再调用 `cuCtxDetach` 返回 201，生命周期仍失败。
- 新增 `--rdy-fd` / `--rel-fd` 已加入 child helper，却没有加入顶层参数解析；四个 exec 子进程打印 usage 并退出，没有执行计算。
- 父进程继续写已关闭的 release pipe，收到 SIGPIPE，未能生成完整错误与回收报告。

后续需修复 context 释放顺序、实际顶层参数入口和早退子进程的 EPIPE 处理。测试必须覆盖这些真实路径；不能靠构造 JSON 的检查器测试代替运行探针。

原生前后 GPU 清单、两份驱动库和输入源码稳定。外层 owner 复核全部 13 个命令进程组为空，无强制清理。`preflight-attempt1/` 保留等待前序 CPU 测试时的调度失败，该次未执行测试命令。`SOURCE-MANIFEST-V4.json` 是作者交付清单，owner 修改后的实际输入以 `owner-report.json.source_sha256` 为准。

使用上级 `recheck-runtime-candidates.py` 可验证归档和源码哈希、布尔序列化差异，以及从原生输出重新得到相同失败原因。原生探针通过后，还必须独立完成内部计算、传输、多卡和生命周期验收。
