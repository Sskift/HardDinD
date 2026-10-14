# 客户端继承共享策略与驱动错误语义

实验补丁 `0023` 接入 NVIDIA 550.120 的 `RM_CONTROL 0xd04`；`0025` 继续修复尺寸与标志位的错误返回差异。原生 Docker、containerd 和 runc 路线保持不变，两个补丁仅进入显式启用的 UML GPU 实验构建。**内部 GPU 计算仍未通过，完整 Docker 功能与近原生性能仍未达标。**

`0xd04` 的规范参数是 12 字节 `RS_SHARE_POLICY`，没有嵌套指针或 FD。转发保持真实 RM 句柄、策略字节和原始指针；沿用页权限同步及 worker 身份。固定驱动源码的 `rmapi/control.c:387–462` 在复制子参数前检查受限标志和规范尺寸，因此 `0025` 将该命令的尺寸/flags 判定交给真实驱动，仍只预触碰规范的 12 字节范围。其他命令的过滤规则不变，未伪造 NVIDIA 状态码。

| 同机实测 | 结果 |
| --- | --- |
| v1 版本查询回归 | 原生前测、关闭桥接、guest、原生后测均通过，启用阶段各 30 项检查 |
| v1 策略边界 | 原生前后各 132/132 检查通过、94 条原始结果一致；guest 131/132 检查通过、80/94 条原始结果匹配，总结果失败 |
| v2 扩展策略边界 | 原生前后各 132/132 检查通过、107 条原始结果一致；guest 131/132 检查通过、105/107 条匹配，总结果失败 |
| v2 内部 Docker | 首次运行、容器重启、两个并发客户端、实例重启后运行，五次均为 105/107 条匹配；五次完整策略验收仍失败 |
| v1、v2 真实 CUDA | 内部两条 `0xd04` 均 rc=0、RM status=0；随后 `0xcb33` 对象分配返回 EOPNOTSUPP、CUDA 错误 304，未执行内部计算 |
| v1、v2 原生 CUDA 前后测 | 每次均完成四卡上传、计算、下载及每卡 1048576 个元素校验 |

v1 的 14 条差异包括未知命令、八种非规范尺寸、四种非零 flags 和继承 FD。v2 消除了原先 12 条尺寸/flags 差异，并新增 FINN 标志、UINT32_MAX 尺寸、无效客户端/指针/尺寸组合、只读子参数和只读头部的错误顺序测试；13 条新增结果均与原生一致。

剩余两条差异是 `0xd0e` 未知命令的 errno/RM status 语义，以及 fork 后子进程使用继承 FD 时的 `EXDEV`。四个子进程分别重新打开设备、分配客户端、设置策略、释放客户端均成功；这与使用继承 FD 不同。策略设置成功仍不能证明共享权限的实际效果，多 UID 凭据、CUDA IPC、设备映射、UVM、多卡进程/线程及显存生命周期仍须完成。

Docker 的无设备、非 root、只读设备权限、配置不匹配、输入不变和正常关机检查通过；25 个实际异步 worker 已核对。全部 29 个记录的实验进程组在收尾审计时为空，两个本轮 supervisor 已退出。28 项仓库检查、固定 NVIDIA 头文件 ABI 编译检查及 155 项取回证据核对通过；这些均不是完整 GPU 或性能验收。

已取回原始报告、源码快照、压缩 stdout/stderr 和 strace，见各同名目录；[evidence-integrity.json](evidence-integrity.json) 保存哈希、原始结果、驱动调用与退出状态核对，[cleanup-audit.json](cleanup-audit.json) 保存远端进程组审计。`gpu-policy-native-v1` 是尚未补齐子进程判定的早期候选，仅保留原始观察；正式 v1/v2 使用后来修正的探针。ABI 检查最初假定远端参考目录包含 Git 元数据而失败，后改用锁定版本的十个传递依赖头文件哈希；失败与成功记录都保留。

已实测不可变内核：

- v1：`360c19f30cb6afce292dc4783aeff5e1ad77952e0cb360d742725c14f8e2ca9a`
- v2：`8672016307a41f75ab9ce6c05daff50a7f1cefd17585dc1632470c855d74d4cc`

复现入口（使用新的实验名，保留旧结果）：

```sh
make kernel-gpu-async-experiment WORK=/root/harddind-rebuild
cp /root/harddind-rebuild/thread-build/linux /root/harddind-rebuild/gpu-policy-repro-linux
python3 scripts/run-gpu-control.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-policy-repro-linux \
  --name gpu-policy-repro --share-policy --async-wait
python3 scripts/run-gpu-control-docker.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-policy-repro-linux \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --probe /root/harddind-rebuild/results/gpu-policy-repro/gpu-guest-probe \
  --native-baseline /root/harddind-rebuild/results/gpu-policy-repro/report.json \
  --name gpu-policy-docker-repro --share-policy --async-wait
python3 scripts/run-gpu-cuda.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/gpu-policy-repro-linux \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --name cuda-policy-repro --async-wait
```

三个运行器当前均应退出 1，因为仍有未实现项。`diagnostic_finished` 表示观察流程完成，CUDA 的 `diagnostic_complete` 表示诊断流程完整，均不能替代内部计算或完整策略一致性通过。追踪数据不用于性能比较。
