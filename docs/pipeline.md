# 开发与实验流程

所有构建、运行、测试和测量只走 `./hd`。一次改动的闭环是：

```sh
./hd check                                  # 本机：单元测试与语法检查，几秒
./hd job ci -- ci --label my-change         # 同步到 Starpub，在 tmux 里构建、功能测试、性能测试
./hd wait ci                                # 等结束，打印评分表
./hd fetch                                  # 拉回运行摘要；完整 ci 运行进入 results/ci/，更新 SCOREBOARD.md 和 HISTORY.md
git add -A && git commit && git push        # 提交改动和它的评分
```

只想快速看一项：`./hd job b -- bench --only 'micro\.|cpu\.' --quick`，或 `./hd job t -- test --only volume`。`--only` 以外的项目记为未测，这种运行只用于定位，不能作为结论。

内核改动的定向对照用 `./hd probe`：同一个静态程序（仓库里的 `.c` 文件会静态编译并缓存）在外层原生、guest A、guest B 中交替运行，guest B 是同一内核加上 `--ab` 参数，用于默认关闭的候选自比较：

```sh
./hd job p1 -- probe --with anon-faultaround --ab uml_faultaround=1 --reps 5 src/core_faults.c anon 65536
```

原始记录在 `runs/<id>/probe.jsonl`（每次运行的退出码、耗时、程序最后一行 JSON 输出）。probe 持有测量排它锁。

提交前钩子：`git config core.hooksPath .githooks` 后，`./hd check` 不通过的提交会被拒绝（新 worktree 需各自执行一次）。

## 位置

| 位置 | 内容 |
| --- | --- |
| 本机仓库 | 源码、补丁、测试定义；`results/ci/` 完整 ci 运行的摘要（入库），`results/runs/` 所有运行的摘要（不入库，`./hd fetch` 拉取） |
| `starpub-root:/root/harddind-rebuild/hd/src/<checkout>` | `./hd sync` 同步的源码，每个 checkout/worktree（按目录名）一份，互不覆盖；含 `.hd-revision.json`，记录 git 版本和是否有未提交改动 |
| `…/hd/work` | 构建暂存：下载缓存、内核源码与目标文件、Go/Rust 工具链 |
| `…/hd/store/<kind>/<key>` | 构建产物。key 是所有输入的哈希；完成的条目不再修改，输入不变就直接复用 |
| `…/hd/runs/<run-id>` | 每次运行的原始日志、实例日志、`summary.json`、`scorecard.md` |
| `…/hd/instances/<name>` | 运行中的 guest，含 COW 磁盘；测试结束即删除，`--keep` 可保留 |
| `…/hd/jobs/<job>` | `./hd job` 启动时复制的源码快照，任务只从这里运行，之后的同步不影响它 |
| `…/hd/logs/<job>.log` | `./hd job` 的输出 |

run id 形如 `20261015-093000-9b3c6733-dirty-bench-label`，包含时间、git 版本、是否带未提交改动和运行类型。

## 内核

- `kernel/series`：默认补丁顺序，全部应用。还不可信的功能放在启动参数后面，默认关闭。
- `kernel/candidates/NAME.patch`：未合入的候选。`--with NAME` 在 series 之后应用，可叠加。候选证明有效后，移入 `kernel/patches` 并加进 series。
- `--kernel-arg K=V`：给本次 guest 加启动参数，例如 `rseq=1`、`gpu_bridge=1`。
- 内核产物的 key 包含 series、候选和配置，切换候选只做增量编译。

## 结果与判定

`summary.json`（`hd.summary/1`）包含版本、构建产物 id、候选、启动参数、宿主负载，以及：

- `test`（`hd.test/1`）：`checks: [{id, status: pass|fail|not_tested, seconds, covers: [docker 子命令…], detail}]`
- `bench`（`hd.bench/1`）：`metrics: {name: {unit, higher_is_better, native: [样本…], guest: [样本…], error}}`

`pipeline/score.py` 按 `bench/targets.json` 评分。`ratio = guest / native`（耗时类取倒数），1.0 即与原生相同。三档门槛：

| 档 | 门槛 | 内容 |
| --- | --- | --- |
| key | ≥ 0.80（用户认可） | 计算、内存、磁盘顺序读写、网络大流量、GPU 计算与传输 |
| important | ≥ 0.50（待确认） | 编译、解包、压缩、元数据、网络请求响应、Docker 控制面操作 |
| other | ≥ 0.20（待确认） | 系统调用、fork/exec、mmap、时钟、线程、管道、信号微基准 |

缺测、出错、跳过都不算通过。功能套件只有全部检查通过才算通过。

### 原生基线

- 负载类指标：同一个基准镜像的 rootfs 在外层容器里解开，用 `chroot` 直接运行，与 guest 里 `docker run` 运行的是同一份二进制。两边绑定同样的宿主 CPU：每次运行开始时从 `/proc/stat` 采样，在同一 CPU 插槽内选最空闲的 4 个物理核（`HD_BENCH_CPUS` 可指定固定列表），记录在 `summary.json` 的 `bench_cpus`。按 原生/guest 交替成对采样，比值取各对比值的中位数（同一对经历相同的宿主负载，可抵消漂移），评分表同时列出每对比值的范围以显示噪声。
- 网络：guest 分别测外层→发布端口、容器→外层、容器↔容器。原生对照取原生 Docker 在同一场景下走的路径：外层回环客户端访问发布端口时，原生 Docker 默认由 docker-proxy 转发，所以 `net.publish_*` 的原生侧是 客户端→docker-proxy（同一份固定 Moby 构建）→服务端；容器出站和容器之间在原生 Docker 里是内核 veth/bridge 路径，原生侧直接用回环。
- Docker 控制面（run、exec、构建缓存等）：Starpub 上没有同机原生 Docker，暂用 `targets.json` 里的参考秒数（服务器上原生 Docker 29 的典型值）。有可用的原生 Docker 主机后再替换。

Starpub 与其他租户共用 72 核，负载常在 40–60，高峰超过 100。外层容器还有 36 核的 CPU 配额（`cpu.max`），容器内总负载超过 36 时，每个 100 ms 周期都可能被限流；guest 靠多个宿主线程相互交接，受限流的影响远大于单个原生进程。每个样本记录了 `throttled`（采样期间被限流的配额周期比例），超过 10% 的指标在评分表中标为 `~noisy`。性能运行持有 `measure` 排它锁，与其他测试、构建不并行；仍应关注评分表里的成对范围，范围很宽时重测，不要据此下结论。

## 并行开发

- 多个代理可以同时改代码、跑 `./hd check` 和 `./hd test`（共享锁）。`bench` 和 `ci` 持有排它锁，同一时间只有一个在跑。
- 代理各自用独立的 git worktree 和分支，`./hd job` 用不同的 job 名；主代理合并到 `main` 后推送。
- 远端只经 fail-closed 别名 `starpub-root-mux`（只复用常驻 ControlMaster，master 不在时立即失败），多步操作合并成一次 ssh；长任务用 `./hd job`，不要在前台占着 ssh。
- 不影响 `/root/udock`。不在 `…/hd` 之外写文件。

## 套件接口

`pipeline/functional.py` 和 `pipeline/bench.py` 各自提供 `run(ctx) -> dict`。`ctx`（`pipeline/runs.py` 的 `Context`）提供：

- `ctx.docker(*args, input=None, timeout=120, check=False)`：外层原版 Docker CLI 连到 guest 的 dockerd，返回 `CompletedProcess`。`ctx.docker_cmd()` 返回 `(argv, env)`，用于 compose 等自己起进程的场景。
- `ctx.artifacts`：`kernel`、`rootfs`、`client`（含 buildx、compose 插件）、`network_probe`、`alpine_tar`（固定的 Alpine minirootfs）、`ids`。
- `ctx.run_dir`：写原始输出的位置。`ctx.log(msg)`。
- `ctx.restart()`：正常关机后用同一块磁盘重启 guest，用于持久化检查。
- `ctx.forward('add tcp 127.0.0.1 HOSTPORT GUESTPORT')`：显式端口转发；`docker run -p` 由发布驱动自动处理。
- `ctx.selected(name)`：是否被 `--only` 选中。
- `ctx.bench_cpus`、`ctx.quick`、`ctx.instance_options`。
- `ctx.share`：外层目录，以 virtiofs 挂载到 guest 内同一路径，可在容器里 `-v ctx.share:...` 做外层 bind mount。
