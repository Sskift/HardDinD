# 时钟与 CPU 查询路径的实际分解

同一静态程序在 Starpub 外层和内部 Docker 中运行，解释原有 CPU 工作量实验中高频查询的开销。原有 `cpu_limit_probe.c` 未改动；该诊断没有减少它的查询频率，也不代替实际 Docker 工作流或性能验收。

原生前后分别运行 1、4 个线程；内部运行 1、4 个线程，再用 `--cpuset-cpus=2,3` 运行 4 个线程。guest 始终是 4 vCPU、4 GiB，保留普通网络、资源控制和外层安全限制，未运行 GPU。每种查询每线程执行 20,000 次、重复 3 次，实际验证返回值、时间单调性和 CPU 编号是否属于允许集合。所有 7 组、285 条线程记录通过这些定向检查，guest 正常关机，28 个记录的进程组为空。

下表是单线程每次循环耗时的中位数，单位 ns。计时使用各自内核的 `CLOCK_MONOTONIC_RAW`，不包含 Docker 启动；4 线程和 cpuset 的全部原始记录在归档中。该短时诊断不构成稳定吞吐或全工作流性能结论。

| 查询 | 外层前参照 | 内部 Docker | 外层后参照 |
| --- | ---: | ---: | ---: |
| libc `clock_gettime` | 19.93 | 4,715.65 | 19.39 |
| 直接 `clock_gettime` 系统调用 | 539.62 | 6,635.07 | 534.17 |
| libc `sched_getcpu` | 3.66 | 4,334.28 | 3.06 |
| 直接 `getcpu` 系统调用 | 486.45 | 5,967.03 | 484.27 |
| libc 时钟和 CPU 查询 | 21.44 | 8,783.55 | 20.64 |

外层 glibc 报告 rseq 注册大小为 32，内部为 0；两侧都存在 vDSO。归档保留实际静态二进制 `sched_getcpu` 的反汇编：它优先读取 rseq TLS，无法使用时退回 vDSO 或系统调用。固定 UML 源码的时间 vDSO 则直接执行系统调用。这些证据支持分别处理时间查询和 guest CPU 编号缓存；不能用宿主 CPU 编号替代 guest 编号，也不能只启用缓存而遗漏迁移、信号和 fork/exec 的语义。

原始证据来自 `/root/harddind-rebuild/clock-query-owner-v1`。`evidence.tar.gz` 保存 25 个实际命令的完整 stdout/stderr、状态和清理记录、编译命令、执行监督程序、探针及关联运行器源码、基线内核配置与补丁清单。`export.json` 固定归档及各成员 SHA256。内核、磁盘和静态二进制留在 Starpub，原始状态记录其摘要；离线复核不会重新执行它们。

```sh
python3 results/starpub-2026-10-15-candidate-review/clock-query-v1/recheck.py
```

探针已保留为 `src/clock_query_probe.c`，Linux 上的实际编译命令为 `gcc -O2 -Wall -Wextra -Werror -static -pthread src/clock_query_probe.c -o clock-query-probe`；参数为线程数、每线程迭代数、重复次数，例如 `clock-query-probe 4 20000 3`。只支持最多 4 个线程，供这轮定位使用。未设置新的性能阈值，整体目标仍未完成。
