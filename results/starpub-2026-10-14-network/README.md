# 无特权 IPv4 网络与持久 Docker 实测

继续使用原版 Docker 29.8.2 / containerd 2.3.6 / runc 1.4.3，以及 Linux 7.2.10 UML。没有修改 Starpub 宿主内核、增加 capability、接入宿主 Docker socket 或修改现有 `/root/udock`。实验内核是含 IPI 空闲修正的 `ipi-kernel-idle`，未启用 mprotect 实验。

`network-v3.json` 的 11 项子集检查全部通过，完整项目仍未通过：Docker 功能 incomplete、性能 fail、内部 GPU fail。

## 实际新增能力

- `harddind.py` 管理独立实例，保留 COW 磁盘，通过真实 Docker API 使用固定版本的原版 CLI；支持 stdin/TTY 和 buildx 入口。
- UML vector fd 通过 Unix seqpacket 连接固定 libslirp 4.6.1，以普通外层 TCP/UDP socket 联网，无 tap、外层 namespace 或防火墙改动。
- 固定摘要 Alpine 3.24.2 镜像从 ECR 冷拉取成功，本轮耗时 8.463 秒；容器 HTTPS 和 BuildKit RUN 均下载真实 Alpine rootfs 并验证锁文件中的 SHA256。
- 显式转发测试在 Docker 发布端口后配置外层转发，校验 TCP 内容与 20 个 UDP 数据报；删除后外层 TCP 端口不再可连接。
- 正常停止通过 UML mconsole CAD、guest tini 信号和 dockerd 退出完成；重启后镜像 ID 相同、restart=always 容器恢复、命名卷中的随机标记逐字相同。

Docker API 仅转发到外层 127.0.0.1 的动态端口。显式转发支持地址选择；目前没有自动跟随 `docker -p`，也没有持久化或重建转发规则。外层 bind 路径、IPv6 流量、异常恢复、多客户端压力及内部 GPU 均未完成。

## DNS 失败与修正证据

`network-v1.json` 保留容器 wget 的 bad address 失败；后续手动重启诊断记录也保留。`network-v2.json` 保留联网 BuildKit RUN 的同类失败。它们不能因第三轮成功而改为通过。

DNS 链路追踪只在指定环境变量时开启，`*.dns.json` 由 `scripts/analyze-dns.py` 解码原始帧。第二轮出现快速 A 响应，但 AAAA 在约 4 秒后返回 SERVFAIL，之后又有接近或超过应用等待期限的响应；这些帧的 IPv4/UDP 校验和有效。独立外层原生探测 `network-native-dns.json` 对默认解析器 127.0.0.11 查询 public.ecr.aws 的 AAAA，连续三次约 4 秒返回 SERVFAIL，证明外层 DNS 本身存在失败和延迟。

`network-resolver-probe.json` 对三个可选解析器、两个域名、A/AAAA、UDP/TCP 共 24 次探测均返回 rcode 0。第三轮明确使用 `--dns 1.1.1.1`，并读取实际容器 resolv.conf 验证配置；没有过滤 AAAA、伪造回答或修改外层 DNS。默认配置仍使用经虚拟地址 10.0.2.3 访问的外层解析器。这解决了本轮联网测试阻塞，不保证任意网络环境或未来 DNS 可用性。

外层直接访问 Docker Hub registry 在此前探测中超时；ECR 可达，因此固定测试镜像来自 ECR。不能据此声称 Docker Hub 拉取已通过。

## 网络性能差距

同一份静态 C echo server 在外层原生和内部 Docker 执行，客户端均在外层，通过回环地址访问。每轮发送 64 KiB 后读回并核验全部数据，共 8 MiB 每方向；顺序为原生／内部／内部／原生，每段三次，各模式共六个样本。所有相关进程限制于 CPU 0–3，但 CPU 非独占，外层负载与 cgroup 压力一并记录。

| 路径 | 每方向有效载荷吞吐中位数 |
| --- | ---: |
| 原生回环 | 9171.04 Mb/s |
| 内部 Docker 发布端口，经用户态网络转发 | 421.68 Mb/s |

该请求／响应负载仍约差 21.75 倍。它同时包含往返等待、UML 网络栈、Docker bridge 和 libslirp，不能当成纯流式 TCP 极限、互联网带宽或 GPU 性能，也没有证明所有成本都来自同一个组件。后端记录 40127 个接收包、79686 个发送包，丢包计数为 0；零丢包不能排除逐包处理、调度和复制成本。

## 复现与证据一致性

```sh
make network WORK=/root/harddind-rebuild
bash scripts/make-runtime-root.sh /root/harddind-rebuild /root/harddind-rebuild/protect-docker-root runtime-dns
python3 scripts/run-runtime.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/ipi-kernel-idle \
  --disk /root/harddind-rebuild/runtime-dns.ext4 --dns 1.1.1.1 --name NEW_UNIQUE_NAME
```

已有基础磁盘和结果文件拒绝覆盖，复现需选择新名称。内核由本仓库固定源码与 `kernel-ipi-experiment` 构建后另存为不可变文件；原始 fixture 与运行器哈希见 `network-provenance.json` 和每轮 JSON。不能使用 mprotect 版本冒充本轮内核。

完成性能测试后才运行 `fixtures/finalize-network.py`，持构建独占锁核对程序、两份磁盘、CLI、guest 初始化脚本、共享库和包版本。13 个相关进程组均无活进程或僵尸；持久 COW 有意保留。现有 udock 只做只读健康检查，返回 server=29.8.2、running=0、images=6。各次启动、停止、失败日志和进程组报告均保留于对应实例目录，没有将宿主健康或正常关机计为完整 Docker 验收。
