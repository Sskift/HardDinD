# 浮点状态缓存候选：任务释放反例

本轮审查未合入的 `0018` 浮点状态缓存候选，发现 runner 保存的任务寄存器指针没有覆盖任务内存的生命周期。在 Starpub 上复用候选作者的原始模型函数，加入真实的 `free()` 后，AddressSanitizer 报告 **heap-use-after-free**。这是用户态模型反例，尚未作为实际内核崩溃复现；它否定了该模型对任务释放安全性的验证结论，因此候选仍不合入主线，也不进行性能验收。

## 可复现的路径

`lifetime_repro.c` 直接包含本目录的 `fp_model.c`，只替换入口，没有重写其状态转换函数：

1. 分配两个任务描述符，使用 `fp_enter(old, runner)` 登记旧任务为 runner 的 owner。
2. 调用候选的 `fp_exit(old)`，然后真实释放旧任务描述符。
3. 使用另一个任务调用候选的 `fp_leave(next, runner)`。
4. `fp_leave` 在检查旧 owner 的状态或 token 之前，先调用 `d_lock(owner)`，向已经释放的内存写入锁字。

原始 ASAN 输出位于 `run.log.gz`：模型的 `d_lock:85`、`fp_leave:232`，释放发生于复现入口的第 12 行。编译成功，运行退出码为 1。原模型使用静态 `tasks[TASK_POOL]`，任务退出后存储仍然存在，无法检出这个释放问题。

对应的候选内核源码也保存在 `fp_lazy.c`。`uml_fp_exit()` 仅将任务设为 EAGER 并清空 `fp_home`，没有清空 runner 的 owner 或保留任务引用；`uml_fp_leave()` 随后取得该指针并调用 `desc_lock(owner)`。同一 mm 仍有其他线程存活时，runner 可以比退出任务的存储活得更久。token 检查发生在解引用之后，不能提供内存生命周期保障。

## 证据范围

- `0018-um-lazy-xstate.patch`、`fp_lazy.c`、`fp_model.c`、`REVIEW.md` 是审查时的候选快照。作者的 REVIEW 不是已通过验收的结论。
- `status.json` 记录编译命令、退出码和源码、产物身份；`compiler-version.txt` 记录远端编译器。
- `evidence-integrity.json` 的 11 项核对通过，含原始模型哈希和 ASAN 调用栈；这不表示候选通过。
- 本次没有运行 guest、Docker、GPU 或性能负载，也没有改变当前主线内核。

在支持 ASAN 的 Linux 环境中进入本目录，可以复现：

```sh
cc -g -O1 -fsanitize=address -fno-omit-frame-pointer -pthread \
  lifetime_repro.c -o /tmp/harddind-fp-lifetime-repro
/tmp/harddind-fp-lifetime-repro
```

修复需要对 owner 存储持有有界且有效的引用，或在释放前可靠解除所有引用，并覆盖 runner 重用、任务退出、mm 销毁、并发接管及地址复用。后续必须对修正后的真实内核运行 FP、线程、信号、ptrace、GPU 与生命周期回归，再安排同机性能对照。不能用避免真正释放内存的模型替代这些验证。
