# HardDinD 的固定目标

在 Starpub 现有无特权 Docker 容器内支持几乎所有 Docker 命令，效率接近同机原生，并提供可用 GPU。用实际命令效果和同机性能判断，不能把目标缩减为少量容易通过的案例。

## 判定

- 性能按 `bench/targets.json` 评分：key 档（计算、内存、磁盘、网络大流量、GPU）≥ 原生 80%，经用户认可；important 档 ≥ 50%、other 档 ≥ 20% 为提议值，用户确认前不得再放宽。
- 改动门槛和原生基线的定义见 `docs/pipeline.md`。要改门槛，必须由用户决定。
- 缺测、失败、跳过都不算通过。`--only` 或 `--quick` 的运行只用于定位。
- 不以 Docker API 兼容、hello-world、nvidia-smi 或单项微基准代替验收。
- GPU 内部验收必须执行计算和传输并校验结果；完整目标还包括多卡、多进程/线程和显存生命周期。原生 GPU 探测与内部容器验收分开。
- 优先级：非 GPU 的 Docker 功能与性能优先（执行核心、资源限制、存储、网络、实际工作流），GPU 次之但不删除。

## 约束

- 不依赖新增宿主权限、关闭外层 seccomp/AppArmor、可写宿主 cgroup、KVM 或宿主 Docker socket。
- 不把关闭网络、忽略资源限制、单核运行、禁用 GPU 称为完成。
- 第三方源码版本固定在 `sources.lock.json`，改动保留为补丁。禁止使用未固定的 latest/master。
- 不影响 Starpub 的 `/root/udock`。所有远端文件放在 `/root/harddind-rebuild/hd/` 下。

## 工作方式

- 所有构建、运行、测试、测量只走 `./hd`（见 `docs/pipeline.md`），不新增一次性的运行脚本或实验目录。新检查加进功能套件，新指标加进基准矩阵和 `targets.json`。
- 一次改动的闭环：`./hd check` → `./hd job NAME -- ci` → `./hd fetch` → 提交改动及其 `results/runs/<id>/`。提交说明写清评分变化。
- 不写逐次实验的叙述性报告。失败用评分表和运行日志记录，`summary.json` 是事实来源；需要解释的结论写进提交说明或 `docs/`。
- 性能测量（`bench`、`ci`）持有排它锁，同一时间只跑一个；构建和功能测试可以并行。
- 直接在 `main` 开发，及时 commit/push；不强推，不覆盖远端改动。并行代理用各自的 worktree 和分支，由主代理合并。
- 可以用 codex（gpt-6-astra）做子代理：给它独立的 worktree 和明确的文件范围，主代理负责审查、集成和远端排程。
- 旧版本：`archive/uml-6.6.158-20261014`（旧 UML 实现）和 `archive/evidence-20261015`（重构前的全部实验证据、一次性脚本和候选）。
