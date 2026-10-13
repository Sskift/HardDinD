# HardDinD

**Hard Docker-in-Docker**：在**没有特权**的容器里跑原版 Docker（无 `CAP_SYS_ADMIN`、seccomp 开着、cgroup 只读、不能建 namespace）。

思路：Docker 依赖的内核功能被宿主拦了，那就自带一个内核。用 User-Mode Linux（UML）把 Linux 6.6 编成一个普通进程，靠 ptrace 截获 guest 的系统调用，里面跑原版 dockerd / containerd / runc。

```
宿主容器 (无特权)
├── docker (shim) ──TLS──▶ 127.0.0.1:23760
├── udock-net  (libslirp 用户态 NAT, 端口转发只绑 127.0.0.1)
│      ▲ 以太网帧 / UNIX seqpacket
└── vmlinux-udock (UML 内核, 单核)
       ├── vec0 10.0.2.15, 网关 10.0.2.2 = 宿主 loopback
       ├── hostfs: 宿主 /root 原路径挂进 guest，所以 -v /root/x:/x 两边同义
       └── Alpine + dockerd 29 (disk.img, ext4, overlay2)
```

## 用法（宿主容器里，root）

```sh
docker ps                 # shim 会自动 udock start，第一次约 10s
udock status|stop|restart|logs
eval "$(udock env)"       # 让别的工具直接用 DOCKER_HOST
```

配置写在 `/root/udock/config`（shell 语法），改完 `udock restart`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEM` | `4G` | guest 内存 |
| `PORTS` | 空 | 要转发到宿主的端口，如 `"8080 5432"`；容器里仍需 `-p 8080:80` |
| `PORT_BIND` | `127.0.0.1` | 上面端口绑定的宿主地址 |
| `SHARES` | `/root` | 原路径挂进 guest 的宿主目录 |
| `PROXY` | `auto` | dockerd 拉镜像用的代理；auto 取宿主 `HTTPS_PROXY`，127.0.0.1 自动换成 10.0.2.2 |
| `DNS` | 空 | 空 = 10.0.2.3（转发到宿主 resolver） |

宿主的 `127.0.0.1:PORT` 在 guest 里是 `10.0.2.2:PORT`。容器要用宿主上的代理，就加 `-e HTTPS_PROXY=http://10.0.2.2:PORT`（build 时用 `--build-arg`）。

## 目录

```
kernel/docker.config   在 UML defconfig 上额外开启的选项 (namespace/cgroup/netfilter/overlay…)
kernel/build.sh        编内核 (顺带把 UML 写死的 HZ=100 改成 1000)
net/udock-net.c        libslirp <-> UML vector(bess) 网卡的桥
guest/udock-init       guest 的 PID 1：挂载、网络、hostfs，然后 tini -> udock-run
guest/udock-run        起 dockerd；收到 SIGINT 时停 dockerd 并关机
build-rootfs.sh        Alpine + docker 磁盘镜像 + TLS 证书
udock, docker          宿主侧管理脚本和 docker shim
test/bench.sh          基准测试
```

安装后的布局：`/root/udock/{vmlinux-udock,disk.img,certs/,bin/,src/,linux-6.6.158/}`，`/usr/local/bin/{udock,docker}` 软链到 `src/`。

## 从零搭建

```sh
apt-get install -y build-essential flex bison bc libslirp-dev uml-utilities e2fsprogs
cd /root/udock && curl -O https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.158.tar.xz && tar xf linux-6.6.158.tar.xz
src/kernel/build.sh linux-6.6.158 /root/udock/vmlinux-udock
src/net/build.sh /root/udock/bin/udock-net
src/build-rootfs.sh /root/udock            # 200G 稀疏文件
# docker 静态 CLI -> /root/udock/bin/docker-cli；compose/buildx -> ~/.docker/cli-plugins/
ln -sf /root/udock/src/udock /usr/local/bin/udock && ln -sf /root/udock/src/docker /usr/local/bin/docker
```

改了 `guest/` 里的脚本：`udock stop && udock update-guest`。

## 实测（72 核 Docker 容器，宿主 Ubuntu 22.04 / 5.15；同一套 Alpine 程序，宿主 chroot vs udock 容器）

| 项目 | 宿主 | udock | 结论 |
|---|---|---|---|
| 纯计算 (awk 2e7 次) | 22.3s | 14.9s | 不慢（宿主还更慢，疑似 seccomp 触发的 CPU 漏洞缓解） |
| 系统调用 (dd 4k×200k) | 0.26s | 8.5s | **慢 ~30 倍**，约 20µs/次 |
| 起进程 (fork+exec×1000) | 0.48s | 10–11s | **慢 ~20 倍**，约 10ms/个 |
| 写 1GB + fsync | 6.1s | 2.8–3.1s | 不慢（guest 的 flush 会对 disk.img 做 fdatasync，确实落盘） |
| 建 5000 小文件 | 0.9–1.1s | 1.2–1.7s | 略慢 |
| 宿主 ↔ 容器网络 | — | ~14 MB/s | 受 libslirp 64KB 窗口限制 |
| 经代理下外网 | 25–230 KB/s | 同量级 | 瓶颈在代理，不在 udock |

启动约 10s，停止约 2s；空闲 CPU 0.4%，内存约 380MB 常驻（随用量涨到 `MEM`）。

## 限制

- 单核：UML 不支持 SMP。
- 系统调用和起进程慢，所以 `apt/apk install`、编译这类负载明显变慢。
- 端口要先在 `PORTS` 里登记，改了要重启。
- 只有 root 能用：证书在 `/root/udock/certs`。
- 不支持 GPU。
