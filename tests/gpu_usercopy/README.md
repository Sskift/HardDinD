# GPU 用户复制和 FD 生命周期的源码夹具

`scripts/check-gpu-usercopy.py` 从指定内核快照原样提取 `gpu_hold_fd`、`gpu_put_holds`、`gpu_check_nested`、`gpu_shadow_snapshot` 和 `gpu_forward_block`，以及它们使用的结构体与常量。编译的测试因此执行候选的实际函数体。

```sh
python3 scripts/check-gpu-usercopy.py \
  results/starpub-2026-10-15-candidate-review/gpu-cuda-lifecycle-v8/source/arch/um/kernel/gpu.c \
  --output /tmp/gpu-usercopy-result.json
```

输出文件必须尚不存在。编译、执行分别有 60 秒、20 秒上限，条件变量等待有 5 秒上限；线程创建、等待和回收错误直接失败。报告记录平台、编译器、源码、头文件、夹具和执行器哈希，以及每条命令的实际退出码和进程组检查。

关键检查：

- 请求 A 解锁后，强制暂停在输出复制之前；请求 B 完成共享影子页覆盖和输出复制之后，A 才继续。两次输出必须各自正确。负对照故意让分配器返回同一缓冲区，必须恰好检出 A 的输出被覆盖；这是注入的别名错误，不是对旧内核的实际复现。
- 实际 `gpu_hold_fd` 取得引用后，在 worker 使用期间关闭并复用同一个 guest FD。worker 必须继续看到原文件的 host FD，用户输出必须恢复 guest FD；原文件只能在 worker 和输出结束后释放一次。
- 使用实际转发和释放函数检查分配失败、输入/输出错误、同步失败、worker 返回错误、部分 FD 获取失败、错误文件类型、不同地址空间、UVM 类型错误、嵌套指针改写与恢复、有效嵌套范围和大小边界。

分配器、用户访问、FD 表、映射同步与 GPU worker 是用户态替身。测试只证明这些假设下的函数行为和确定性交错，**不证明真实内核缺页、VFS、GPU、OOM 或长期并发已经通过**。它也未覆盖事件分配路径、任意损坏的内部描述表和完整 ioctl ABI。Starpub 的旧、新内核真实对照仍必须单独完成。
