# 实际 Docker 工作流计时

`scripts/run-workflow-timing.py` 在独立的持久实例中调用原版 Docker CLI，记录完整工作流及每个命令的耗时和实际结果。它补充命令功能套件与内核微基准，不代表完整兼容性或原生性能验收。

| 分组 | 每次重复的操作与结果检查 |
| --- | --- |
| `lifecycle` | create/start/logs/exec、与 stop 同时等待的 wait、再次启动、restart、rm；检查退出码、容器身份、启动时间变化和文件保留 |
| `image` | 固定本地 rootfs 的 import、复制数据后 commit、save/rmi/load；检查镜像 ID 和加载后实际读取的文件摘要 |
| `volume` | 创建命名卷、docker cp 写入、两个容器读取、cp 回外层；检查 1 MiB 数据长度及 SHA256 |
| `build` | 固定本地构建上下文的 `--no-cache` 构建和缓存重建；比较文件层与运行配置，在两个容器内重新计算文件摘要，保留默认构建证明及缓存输出 |
| `net` | 创建 bridge、服务容器、客户容器；将 DNS 应答与服务实际地址核对，再请求 HTTP 内容 |

每个分组默认重复三次。内核、基础磁盘和 rootfs 必须明确固定；rootfs 使用 `sources.lock.json` 的版本及 SHA256，不在线拉取镜像。每次运行使用不同实例名，运行器拒绝覆盖已有实例和证据。

构建证明元数据可以让两次构建的顶层镜像索引 ID 不同，即使实际文件层与运行配置相同。检查器保留这两个 ID，核对实际执行的镜像内容，不关闭构建证明来强行取得相同 ID。网络服务使用固定 BusyBox 已提供的 `nc`，不假设最小 rootfs 包含 `httpd`。

```sh
python3 scripts/run-workflow-timing.py /root/harddind-rebuild/workflow-run \
  --kernel /path/to/fixed/linux --disk /path/to/fixed/runtime.ext4 \
  --rootfs /root/harddind-rebuild/cache/alpine-minirootfs-3.24.2-x86_64.tar.gz \
  --name workflow-lifecycle-01 --shards lifecycle --reps 3 --cpus 4 \
  --workload-budget 120
```

工作目录需要已有的 `client/`、`network-backend` 和指向全局锁的 `kernel-build.lock`。主代理串行安排测量，禁止同时构建或运行其他实验。运行器持有共享构建锁以阻止构建改写输入；该锁本身不保证多个测量互斥，也不能排除宿主其他任务负载。启动、单命令、工作阶段与关机分别有超时；工作阶段预算不是整次运行的总时限，外层监督器还应负责总超时和最终实例清理。

输出位于工作目录的 `results/NAME.json`、`NAME.log` 和 `NAME-raw/`。每个命令保留参数、退出码、超时、原始 stdout/stderr 文件及摘要、独立进程组清理记录。运行前后核对输入哈希；只删除本次注册的对象，并检查带本次标签的残留。运行失败、超时、预算不足和未执行的重复均保留。预期的 inspect 未找到对象必须核对实际错误内容，不能把任意退出码 1 当作删除成功。

命令计时包含 CLI 进程及现有 Python 入口的开销。工作流计时还包含校验命令、必要等待和记录操作；`wait` 与 `stop` 有意重叠，不能将所有分项直接相加。失败尝试也保留耗时和计数，不用缺失的零秒样本压低统计。单次 `--reps 1` 只可用于定位，不能形成三次重复的证据。

`scripts/analyze-workflow-timing.py REPORT... --output COMPARISON.json` 可汇总不同固定内核的报告，并检查 rootfs、磁盘、载荷、脚本、运行配置、亲和性及 daemon 版本是否一致。输入不一致时返回非零。结果只提供观测值，没有性能容差或自动排名。

当前 Starpub 没有可用的匹配原生 Docker daemon 供本工具对照；`native_docker_comparison` 固定为 `not_tested`，`near_native_acceptance` 与 `full_acceptance` 保持 false。Compose、外层发布端口、registry push、更多 buildx 功能及其他命令仍需各自验收，不能从这五类工作流推导为全部通过。

仅验证工具自身的传输、失败判定与清理路径：

```sh
python3 -m unittest discover -s tests -p 'test_workflow*.py'
```
