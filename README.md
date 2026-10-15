# HardDinD

在 Starpub 的无特权 Docker 容器里运行完整 Docker：外层不给 user/mount/net namespace、没有 KVM、不能改宿主内核，所以用改过的 UML（User-Mode Linux）内核作为普通进程运行，在其中运行原版 dockerd/containerd/runc。namespace、cgroup、overlayfs、iptables、bridge 都由 UML 内核真实实现。

**状态：开发中，未达标。** 当前评分见 `results/runs/` 最新的 `scorecard.md`。

## 组成

| 部分 | 位置 |
| --- | --- |
| UML 内核：Linux 7.2.10 + `kernel/series`（seccomp 交接、多 vCPU 并行执行、映射管理、virtiofs 中断、GPU 控制通道等） | `kernel/` |
| 网络后端：固定 libslirp 4.6.1 + IPv6 分片补丁，外层回环端口转发 | `src/network_backend.c`、`third_party/` |
| `docker run -p` 自动发布：Moby 补丁 + guest 内端口代理 | `moby/patches/`、`src/port_agent.go` |
| 外层目录共享：virtiofsd 1.13.2 + 共享锁补丁，经 vhost-user 接入 UML | `virtiofs/patches/` |
| guest 启动与 dockerd 监督 | `guest/` |
| 流水线：构建缓存、guest 实例、远端任务、功能套件、基准矩阵、评分 | `pipeline/`、`./hd` |
| 性能门槛 | `bench/targets.json` |

## 使用

```sh
./hd check                          # 本机快速检查
./hd job ci -- ci                   # 在 Starpub 上构建、功能测试、性能对照
./hd wait ci && ./hd fetch          # 等结果并拉回摘要

./hd remote up dev                  # 交互用的 guest
./hd remote docker dev -- run --rm alpine echo hi
./hd remote down dev --rm
```

开发流程、产物位置、评分规则和原生基线定义见 [docs/pipeline.md](docs/pipeline.md)，技术路线和已知瓶颈见 [docs/architecture.md](docs/architecture.md)。

重构前的实验记录在标签 `archive/evidence-20261015`，更早的实现在 `archive/uml-6.6.158-20261014`。
