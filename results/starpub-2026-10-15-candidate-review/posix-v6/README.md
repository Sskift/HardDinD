# POSIX 共享锁 v6：原生和内部 Docker 各 47 项通过

Starpub 的实际运行完成原生 47/47、内部 Docker 47/47。guest 正常关机，133 个记录的进程组均为空，没有强制回收；源码、后端和独立动态库哈希前后稳定。此次没有运行计时基准，`benchmark_complete=false`、`full_acceptance=false`。

v5 的唯一内部失败来自测试探针调用镜像中不存在的 `/bin/sleep`，且旧就绪消息在 exec 前发出。v6 改成静态探针自身的 exec helper：新程序启动后发送 PID 和仍打开的锁 FD；另一进程随后验证冲突，受控退出后验证重新获取。增加的失败路径实际确认 ENOENT、退出码 127 和锁释放。原生与内部均通过这两个 exec 用例。

其余实际检查包括共享/独占 flock、跨层 POSIX 范围冲突、单字节和 EOF 边界、大偏移、部分解锁、读写锁转换、多次打开和 close/dup/fork 生命周期、信号与杀进程取消、guest 持锁 PID、外层持锁者 PID 为 0、单文件及跨文件 guest 死锁检查、rename/unlink。

后端与 v5 完全一致：`13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`；补丁 SHA256 为 `a7ffeca5fd5b847879d92b0f160035c88eee5440d6c95dd9b62b8c44ad51c37c`。owner 在作者收尾时独立冻结三份稳定源码，随后作者最终报告中的哈希与冻结版本一致。owner 重新编译探针后执行真实双侧测试，保留独立动态库的实际进程映射。

该补丁和已实测的启动入口、探针、测试脚本现纳入主线。构建后端后，启动共享目录时使用 `--share-posix-locks`；回归命令为：

```sh
python3 scripts/run-shared-locks.py WORK \
  --kernel KERNEL --disk DISK --name posix-regression \
  --virtiofsd WORK/virtiofsd --posix-locks --inline-flock --functional-only
python3 results/starpub-2026-10-15-candidate-review/recheck-runtime-candidates.py
```

`--functional-only` 不生成性能结论。混合外层/guest 的阻塞死锁环、跨 PID namespace 的 GETLK 身份映射、共享 mmap、通知、异常恢复与完整 I/O 性能仍未完成；这些缺口没有因本轮检查通过而消失。先前 v1/v2/v5 的失败和缺测保持原样。
