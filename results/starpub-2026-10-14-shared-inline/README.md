# 文件锁分派优化实验

当前未完成，不能声明性能改善或生产可用。`shared-inline-v1` 原生 21 项通过、Docker 19 项通过及 2 项 POSIX 范围锁失败；**关机还出现 45 秒超时，最终强制结束 guest**。失败证据完整保留，未纳入有效性能对照。

## 改动

`virtiofs/patches/0004-inline-flock.patch` 将 virtqueue 锁的作用域缩短到取出描述符。原来的 while-let 条件临时值会持有锁直到循环体结束，工作线程完成回复也需要同一把锁。

可选 `--inline-flock` 在事件线程中尝试一次非阻塞宿主 flock；数据表或文件内部锁忙时返回异步路径，真正发生文件锁竞争的阻塞请求继续进入可取消的等待队列。普通 I/O 仍使用 4 个工作线程。新增 Rust 检查实际持有内部写锁以及独立宿主 flock，验证不会把内部锁忙误报为 guest 文件锁冲突，也不会绕过外层锁。固定构建的 5 项补丁检查通过。

运行器通过 `--share-inline-flock` 启用，默认关闭。`--virtiofsd PATH` 可以选择不可变的服务二进制，路径和 SHA256 都记录到实例输入，便于对照不同版本。新服务 SHA256 为 `82743fb83d42c5e63633b607f26fb84a2ed2b8a20e7ca2b1375272f5d54d1790`；旧服务为 `bd689aefb17d8a454f4f587c47d342f0b5c5686f96cd07cbee69cbb7b92a8a1b`。两者都使用固定 virtiofsd 1.13.2 和 Cargo.lock。

## 首轮失败

计数确认 12036 次请求直接完成，9 次进入等待队列，内部锁忙回退为 0；取消队列的 2 次中断均被处理，退出日志中 remaining=0。19 项通过的检查包括双向锁排他、dup/fork、正常关闭、强杀与信号取消、6 个等待者及只读 bind mount。

guest 已输出 `HARDDIND_RUNTIME_STOPPED`，但没有在 45 秒内退出。记录中的 guest 退出码为 -9、forced_shutdown=true，文件服务退出码为 1；最终进程组没有残留。由于停止失败，没有执行最后一组原生样本，`benchmark_complete=false`。不能用本轮约 0.279 秒的 Docker 中位耗时宣称比前轮更快。

目前未证明超时由这次分派修改导致，也未证明与它无关。运行器现在在关机超过 15 秒时请求 guest SysRq 任务/CPU 栈，并记录本实例宿主进程组的 stat、wchan 和 syscall；保持原有 45 秒截止时间，读取失败也保存错误。后续复现必须检查这些证据，不能靠增加超时或重测通过覆盖首轮失败。

## 对照入口

`scripts/compare-shared-locks.py` 按旧、新、新、旧顺序串行运行，无插桩，每阶段都有同机原生前置与后置各 3 个样本、Docker 6 个样本，使用相同内核、磁盘、探针和 CPU 0–3。此时 `run-shared-locks.py --benchmark-only` 的功能检查明确保持 not_tested，不把计时阶段算成功能验收。任何阶段输入变化、退出失败、缺样本或缺少真实 inline 路径计数都会使比较失败。

```sh
python3 scripts/compare-shared-locks.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/virtiofs-interrupt-kernel-v1 \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --baseline /root/harddind-rebuild/virtiofsd-profile-v1 \
  --candidate /root/harddind-rebuild/virtiofsd-inline-v1 \
  --name new-inline-abba
```

该对照还没有完成。宿主负载不受本实验独占控制，样本不能代表完整 Docker 应用性能；GPU、POSIX 锁、共享 mmap 和通知等缺口仍保留。
