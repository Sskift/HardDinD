# POSIX 锁候选 v5：原生 46/46，内部 Docker 45/46

在 Starpub 实际执行原生和内部 Docker 对照，启用共享 POSIX 锁及 inline flock。原始结果见 [posix-v5.json](runtime/posix-v5.json)。全部 92 个功能案例已执行，内部 `posix_exec_preserves` 失败，因此整轮失败；没有运行性能基准。

本轮包含字节范围、独立进程、同进程多次打开、关闭重复描述符、fork、部分解锁、读写锁转换、阻塞与 EINTR、进程死亡释放、GETLK PID、单文件和跨文件 guest 死锁、exec、只读权限、取消/授权竞争、大偏移、rename/unlink。原生 46 项通过，内部 45 项通过。

唯一失败返回 `exec-reap rc=-1 errno=0`。实际导入镜像仅包含 `/share-probe`，探针却执行 `/bin/sleep`，镜像里没有这个程序。原探针又在 exec 前发送就绪消息，因此此前观察到的冲突不足以证明 exec 后锁保留。后续需自身执行的静态 helper，执行成功后握手，再检查锁和有界回收；不能把当前失败改写成通过，也不能用这次失败证明锁后端本身有错。

候选 virtiofsd SHA256 为 `13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`，补丁为 `a7ffeca5fd5b847879d92b0f160035c88eee5440d6c95dd9b62b8c44ad51c37c`。`virtiofsd-maps.txt` 记录实际进程加载的是 owner 目录中固定的 libcap-ng/libseccomp；输入和库哈希前后不变。guest 正常关机，135 个记录进程组独立复核均为空。

`author-evidence/` 单独保留作者的 Rust、原生扩展和旧竞态反例验证；它们不能代替本轮内部 Docker 验收。混合宿主/guest 死锁、跨 PID namespace 的持锁者归属以及其他未覆盖语义仍有限制，候选未合入正式后端。

`repo/` 是冻结源码，包含本轮 owner 对命令超时及进程组清理的修改。本归档不包含 ELF、磁盘或镜像 tar。使用上级 `recheck-runtime-candidates.py` 验证哈希、实际结果和清理记录。
