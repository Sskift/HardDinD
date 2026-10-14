# UML memfd 内存与启动程序后端

**完整 Docker、接近原生性能和内部 GPU 目标仍未通过。** 本轮默认补丁 `0015` 提供两个独立、默认关闭的运行开关：`memfd_ram=1` 将 UML 物理内存文件改成共享可执行 memfd；`memfd_stub=1` 要求用户进程启动程序保存在只读封印的 memfd 中。

上游启动程序路径原本调用 `memfd_create(MFD_EXEC | MFD_CLOEXEC | MFD_ALLOW_SEALING)`，Star­pub 的 Linux 5.15 不支持该新标志而退回 ext4 临时文件。新路径只在 EINVAL 时重试旧接口；权限拒绝不会触发这个重试。启动程序写入完成后检查全部封印，阻止继续写入和改变大小。RAM memfd 仍保持可写，实际检查可执行映射，不改变外层权限、seccomp、AppArmor 或挂载配置。官方接口语义见 [Linux memfd 文档](https://docs.kernel.org/userspace-api/mfd_noexec.html)。

两个开关与原 GPU、SMP、cgroup、网络和磁盘功能独立。运行器记录实际 RAM 分配与启动程序后端，并拒绝把只有命令行参数、没有内核确认的运行算作已启用；持久实例保存配置，重启或复用时核对。没有默认启用这两个实验选项。

## 构建与首次实测

第一次编译因引用了当前源文件未声明的 `fatal()` 失败，保留失败补丁和日志；改为明确输出错误并退出，第二次编译和 stub 页外重定位检查通过。

内核 SHA256：`d872149b71ff0026201bebe5a8f482bb883a3c6c47c56d02d4411e444766bc5a`。

`memfd-smoke-v1` 在两个开关均启用时完成 3 组映射语义、5 组线程语义和 36 个样本，退出正常，无强制清理或残留。日志确认物理内存与启动程序均使用 memfd。可写映射、保护、COW、进程创建及多线程不是通过关闭功能来绕过的。

## 四组合对照

`memfd-factor-v1` 使用同一份不可变内核与磁盘、相同程序、CPU 0–3、4 vCPU、2 GiB 请求内存、spin=256、broker cache 和 deferred IPI。顺序为原生／file／ram／stub／both／both／stub／ram／file／原生；各模式各项六个样本，共 360 个测量值。八次 guest 均完成映射和线程检查，正常退出，无失败样本被排除。

下表为本轮中位数，单位毫秒。file 是两个开关都关闭，ram/stub 是单独启用，both 是同时启用。

| 负载 | 原生 | file | ram | stub | both | both / 原生 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 万次 getpid | 78.828 | 478.721 | 417.913 | 424.211 | 386.769 | 4.91× |
| 100 次 fork/exec/wait | 60.021 | 245.758 | 174.439 | 281.447 | 168.969 | 2.82× |
| 512 轮映射/触页/保护/释放 | 21.249 | 306.993 | 352.004 | 410.250 | 173.993 | 8.19× |
| 四线程 8 万次身份系统调用 | 32.835 | 274.993 | 285.878 | 451.908 | 212.288 | 6.47× |
| 四线程 96 次 fork/exec | 31.117 | 434.695 | 419.287 | 367.546 | 251.667 | 8.09× |

both 相对 file 的串行 fork/exec 耗时下降约 31.2%，映射下降约 43.3%。但单独启用的某些组合变慢，纯计算和宿主状态也有波动；CPU 没有独占，不能把该轮结果直接认定为稳定的因果收益。UML 的 ASLR/exec-shield 间隙导致日志中的物理文件长度随启动变化，实际分配长度已记录，不能把文件长度直接当作 guest 可用内存。

原始日志、每个样本、资源状态和源码哈希都保存；原始比较器快照为 `memfd-factor-v1-compare.py`。带逐次原生对照的后续复测与 Docker/GPU 回归尚待汇总。微基准仍有明显原生差距，不构成项目性能验收。

## 复现

```sh
make kernel-gpu-experiment WORK=/root/harddind-rebuild
# 复制成功构建的内核到独立路径，避免运行中重建。
python3 scripts/compare-memfd.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name new-memfd-factor
python3 scripts/compare-memfd.py /root/harddind-rebuild \
  --kernel /path/to/immutable/kernel --name new-memfd-bracket --pairwise
```

持久运行器和 core/Docker/GPU 测试入口接受 `--memfd-ram`、`--memfd-stub`。内部 CUDA、GPU 映射、完整共享目录和网络性能等原有缺口仍在范围内。
