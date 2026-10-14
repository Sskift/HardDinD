# NVIDIA 设备调用调查

这是后端实现前的外层 ABI 调查，**内部 Docker 的 GPU 仍未实现或通过验收**。

在共享锁测量结束后串行运行 `scripts/trace-gpu.py`。程序从仓库的 `src/cuda_probe.c` 重新编译，在每张卡上创建上下文、分配三块显存、上传两组数据、执行 PTX 加法、下载并逐项核对 1048576 个结果，随后释放显存、模块和上下文。4 张卡均通过这一次顺序计算；进程退出码 0、未超时，专用进程组没有强制清理或残留。

实际模块和用户态驱动为 NVIDIA 550.120；报告保留程序、源码、工具脚本、实际打开的 libcuda/PTX JIT 库以及原始 trace 的 SHA256。没有安装或替换宿主驱动，也没有调整设备权限。

## 观察到的接口

- 共 2725 次 NVIDIA ioctl 入口，按设备路径与请求码分为 57 组。最常见的 nvidiactl 请求为 `0x46/0x2a`、`0x46/0x29`、`0x46/0x2b`；目前只记录请求编码，没有据此宣称已理解其内嵌对象与指针。
- 设备 mmap 入口共 128 次：nvidiactl 116 次，nvidia0–3 各 2 次，nvidia-uvm 4 次。其中存在 MAP_SHARED 和指定地址的 MAP_FIXED 映射。因此后端还需要设备内存映射能力，不能只实现 ioctl 返回值。
- UVM 有编码大小为 0、仍传递用户地址的 ioctl。单凭 ioctl 编码中的 size 无法推导需要复制的数据结构，下一步需要按固定驱动版本解析接口结构和指针/FD 引用。
- 记录到 5 次 clone3 和 5 次 clone 入口。入口计数包含失败调用，不能把它们相加当成成功创建的线程数。进程/线程、并发多卡、锁页内存和显存生命周期压力仍未验收。

`gpu-abi-v1-inventory.json` 由 `scripts/analyze-gpu-trace.py` 生成。分析器验证报告与压缩 trace 的哈希，处理 strace 的嵌套设备注记以及 unfinished/resumed 配对，检查解析出的 ioctl 总数与采集报告一致。原始 trace 保留在 `gpu-abi-v1-syscalls.strace.gz`；计数包括失败调用，不表示每个调用都成功。

## 复现与边界

```sh
python3 scripts/trace-gpu.py /root/harddind-rebuild --name new-gpu-trace
python3 scripts/analyze-gpu-trace.py REPORT.json TRACE.strace.gz
```

采集器使用专用名称并拒绝覆盖现有结果；持有实验构建锁以避免与本仓库的 guest 测量重叠。strace 会改变时序，报告中的 CUDA 耗时只作诊断，不作为 GPU 性能基线。外层真实计算成功不是内部 Docker 成功，这份调查也没有实现 guest 到 NVIDIA 驱动的 ioctl、mmap 或 UVM 转发。
