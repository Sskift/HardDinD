# HardDinD 的固定目标

在现有无特权 Docker 容器内提供完整 Docker 功能、与同机原生执行一致的性能和可用 GPU。用户没有授权降低这三个目标。

- 不以 Docker 兼容 API、hello-world、nvidia-smi 或单项 CPU 测试代替最终验收。
- 不依赖新增宿主权限、关闭外层 seccomp/AppArmor、可写宿主 cgroup、KVM 或宿主 Docker socket。
- 不把关闭网络、忽略资源限制、单核运行、禁用 GPU 称为完成。
- 原型可以暴露缺口，但必须明确标为未完成。缺测、失败、跳过均不能算通过。
- 性能阈值必须经用户认可；不得自行把“一样”改为慢 10% 或慢一倍。
- 原生 GPU 探测与内部容器 GPU 验收分开。内部必须执行计算、传输并校验结果；完整目标还需要多卡、进程/线程和显存生命周期测试。
- 每个第三方源码版本固定，改动保留为可复现补丁。禁止依赖未固定的 latest/master。
- 不影响 Starpub 已安装的 `/root/udock`。实验使用 `/root/harddind-rebuild`，独立磁盘、端口和实例名。
- 旧实现可在 Git 标签 `archive/uml-6.6.158-20261014` 找到。不要再把它当成已达标的后端。
