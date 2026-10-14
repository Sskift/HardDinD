# 外层共享目录实测

结论：真实 Docker 容器已经可以 bind mount Starpub 内的外层目录，但文件语义和性能仍未达标，GPU 未在本轮实现或验收。没有改变外层 seccomp、AppArmor、设备权限或宿主配额，没有使用宿主 Docker socket。

固定 Linux 7.2.10，新增 UML virtio/vhost-user 与 virtiofs；服务固定 virtiofsd 1.13.2 的源码提交及锁文件，构建器校验 Rust 1.98.1 和本地解包的原生依赖。内部仍运行真实 Moby/containerd/runc。普通 Docker 容器使用 `--mount type=bind`，未使用 `--privileged`。

## 结果与原因

| 运行 | 原生 | 内部 Docker | 实例结束 |
|---|---|---|---|
| shared-auto-v1，上游服务 | 16 pass / 2 fail | 18 not_tested | 启动失败，强制回收 guest |
| shared-auto-v2，补丁服务，cache=auto | 16 pass / 2 fail | 8 pass / 10 fail | 正常关机 |
| shared-never-v1，补丁服务，cache=never | 19 pass / 2 fail | 14 pass / 7 fail | 正常关机 |

第三轮新增两项 UID 1001 通知对照和一项并发创建检查，不可直接用通过数量比较两个缓存策略。原生的只读基线使用 O_RDONLY FD，内部检查实际只读 bind mount，两者不是相同的挂载测试。

初始阻碍为 `unshare(CLONE_FS)` 返回 EPERM。上游用它隔离每个工作线程的 cwd 与 umask。补丁 `virtiofs/patches/0001-shared-fs-state.patch` 保留 4 个线程，对临时 cwd 与创建掩码分别加短作用域锁；普通 fd 相对操作和读写不持有这两把锁。Rust 的 8 线程并发目录/权限检查各循环 256 次，通过；真实内部 Docker 的 8 进程、共 512 次创建/权限核对/删除也通过。构建器每次从校验后的归档重新解包、无模糊偏移地应用补丁，避免缓存源码修改混入构建。

`auto` 的具体失败：已打开 FD 读到外层写入前的数据；fstat 大小/权限陈旧；重新打开替换/重建后的路径仍指向旧 inode；mmap 看不到外层修改。固定源码 `src/main.rs` 中 auto 的属性/目录项超时为 1 秒，文件使用 guest 页缓存。`never` 关闭这些缓存后，普通读取、fstat、替换与删除后重建均通过。两种模式的非 root 拒绝访问、创建所有权、xattr 双向传递、内部写入后外层读取和只读 bind 检查均通过。

剩余差距都有独立失败证据：

- **共享 mmap**：auto 看不到外层写入；never 双向 MAP_SHARED 均返回 ENODEV。Linux `fs/fuse/file.c` 的 direct-I/O 映射路径明确要求 `FUSE_DIRECT_IO_ALLOW_MMAP`。强行开启该位并不能消除两个内核的页缓存差异，因此未用它伪造通过。
- **跨层 flock**：外层持锁时内部仍能取得排他锁，反向也如此。固定 virtiofsd 的 `FileSystem::getlk/setlk/setlkw` 仍是 ENOSYS 占位，PassthroughFs 未协商远端锁；Linux `fuse_file_flock` 在 `no_flock` 时仅使用 guest 本地锁。后续需实现锁所有权、阻塞/取消、关闭与进程退出清理，不能只转发非阻塞调用。
- **外层到内部 inotify**：外层写入后，guest 通知队列没有事件；root 与 UID 1001 都复现，不能归因于 root 的宿主配额。
- **外层 root 通知配额**：独立进程仅打开 4 个 FD，RLIMIT_NOFILE 为 1024，UID 0 的 inotify_init1 返回 EMFILE，UID 1001 成功；当前 max_user_instances 为 128。这支持 UID 相关资源限制的判断，但未盘点宿主全部实例。原生 root 两项仍记 fail。补充测试显示内部写入可触发外层 UID 1001 的 inotify，不能用该补充结果覆盖 root 失败。

尚未测试跨层 Unix socket/FIFO、挂载传播、ACL 实际授权、共享目录作为 overlay upperdir、多客户端压力、异常恢复等完整行为。没有把这些项目计为通过。

## 同机性能

每轮使用同一份静态程序和相同 CPU 0–3，CPU 不独占。按原生前置 3 样本、Docker 两组共 6 样本、原生后置 3 样本串行执行。每个样本按 64 KiB 块写/读 32 MiB；写入包含 fsync，读取逐字节校验。下面是各轮中位吞吐，单位 MiB/s。

| 模式 | 原生读 | Docker 读 | 原生写 | Docker 写 |
|---|---:|---:|---:|---:|
| auto | 1061.917 | 411.717 | 125.601 | 53.562 |
| never | 1152.568 | 322.718 | 145.085 | 52.597 |

never 本轮读/写分别约为原生的 28.0% / 36.3%。auto 的较高读取速度伴随文件一致性失败，不能作为达标路径。两轮并非交错 A/B，对共享宿主噪声和冷暖缓存没有完全控制，不能把二者差值都归因于缓存选项。这里只是特定文件 I/O 对照，不是完整 Docker 性能，也没有自行设定用户认可的性能阈值。

## 复现与证据

先按仓库构建 UML、virtiofsd、网络、Moby 和端口 agent，再用 `make-runtime-root.sh WORK BASE runtime-shares --with-ports` 制作包含共享挂载初始化的新磁盘。运行程序会拒绝复用既有实例/结果名称。

```sh
bash scripts/build-share-probe.sh /root/harddind-rebuild
python3 scripts/run-shared-files.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/virtiofs-kernel-v1 \
  --disk /root/harddind-rebuild/runtime-shares-v1.ext4 \
  --name new-shares-run --cache never
```

该命令应因未修复的语义返回非零。JSON 保存逐项状态、错误、程序/脚本/内核/磁盘哈希、所有性能样本和 runtime 退出状态；日志保留实际 Docker 命令及探针响应。早期启动失败记录未被覆盖。`virtiofsd-build-v3.log` 保存补丁服务的构建与并发检查，`kernel-build-v1` 只代表内核构建成功。

`cleanup-v1.json` 再次检查了本轮 9 个专用进程组，全部没有活进程或僵尸。两个完成测试的实例没有强制关机；首次启动失败的强制回收仍保留失败状态。原有 `/root/udock` 只作只读健康检查，服务为 29.8.2、运行容器 0、镜像 6，未修改它的文件或配置。
