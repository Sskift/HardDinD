# GPU 映射候选 v4：实际启动暴露设备注册回归

完整候选内核已在 Starpub 构建并实际启动，但启用 GPU 桥接的验证失败。内核 SHA256 为 `089e37cf9e0c02a7c3e8777afde2bae6524931da0477fe9209544e1f7f78e7fa`，新增补丁 SHA256 为 `34a791413d3cf185e850ecee2f314224f0f755320cd8f60ac535ddf8903fd360`。补丁从已经验证的 `0017`、`0025/0026/0027` 基线生成，放在该基线之后；远端 `patch --fuzz=0` 重放后，14 个目标源文件全部与冻结候选相同。它仍是失败候选，没有替换主线默认内核。

按固定顺序运行系统特性查询的原生前对照、关闭桥接的 guest、开启桥接的 guest、原生后对照：原生前后各 31 项观察一致，关闭桥接符合预期，开启桥接则没有启动 GPU worker、没有执行设备 ioctl，也没有产生应有的 31 项观察。四个阶段均正常退出，独立复核的四个进程组均为空。**guest 进程退出 0 不等于功能通过**；原始判定明确为 fail。

失败位置已经从实际日志和对应源码对上：

```
sysfs: cannot create duplicate filename '/dev/char/195:0'
... device_create ... gpu_init ...
GPU_CONTROL_DISABLED status=0
```

`gpu_init` 为标准 `nvidia0` 和兼容 `harddind-nvidia0` 调用了两次 `device_create`，传入同一个 `dev_t`。第二次注册试图创建重复的 sysfs 设备号链接并失败，设备枚举在第一张卡处中止，尚未注册控制节点与 UVM 节点。失败回收中的 `while (index--)` 还漏掉当前索引；这部分是冻结源码发现，不冒充已单独执行的故障注入结果。

后续修复需给兼容节点独立的设备号与 cdev，并按成功注册状态逐个回收当前及之前的节点。标准节点、兼容节点和真正的内部 CUDA 都需重新实测。控制回归没有通过，因此本轮没有继续执行 alloc 或 CUDA；它们记为未测，没有把“probe 没运行”解释成 CUDA 计算失败。

`source/` 和 `baseline/source/` 保存对应源码，`kernel.config`、`build.log`、补丁、重放输出和哈希清单保存构建背景。原始运行器额外加入了本补丁的归档；CUDA 运行器预备版增加了编译锁，但本轮未执行。`runtime/` 保留实际运行器、探针源码、四阶段日志和压缩系统调用轨迹。151 MB 内核、磁盘、探针二进制与整个工作目录没有收录，远端完整清单也包含未收录文件；不能把该目录称为完整独立构建包。

以下命令只重算源码差异、哈希、原生查询结果、失败条件和清理证据，不启动 VM：

```sh
python3 results/starpub-2026-10-15-candidate-review/gpu-mapping-v4/recheck.py
```
