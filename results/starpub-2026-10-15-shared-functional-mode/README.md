# 共享锁功能回归模式

为共享锁运行器增加 `--functional-only`，在并行开发期间检查功能而不产生受资源竞争影响的性能采样。这个模式保留原生与内部 Docker 的全部功能案例、输入哈希、进程清理及 inline 执行证据要求；`samples` 为空，`benchmark_complete` 为 false。退出成功还要求 `functional_complete` 和全部功能案例通过。它不能与 `--benchmark-only` 一起使用。

Starpub 上 `make check` 通过；CLI 帮助退出 0，冲突参数在任何实验创建前退出 2。`report.json` 记录运行器 SHA-256，`checks.log.gz` 保留完整检查输出。此目录仅证明仓库及参数检查，不表示共享锁功能或性能已达标。POSIX 字节范围锁候选仍在独立目录审查和实测，尚未纳入 main。
