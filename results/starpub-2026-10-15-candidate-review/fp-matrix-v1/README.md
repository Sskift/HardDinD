# 浮点候选 v4：真实 guest 正确性对照

Starpub 在 2026-10-15 完成原生 15 项、12 组 guest 配置各 15 项检查，均无失败或跳过。15 个命令进程组正常退出，独立清理复查为空。此轮只验证探针覆盖的行为；候选尚未合入主线，完整功能和性能验收均为 false。

| 内核 | guest CPU | 并行 runner | lazyxstate | 结果 |
|---|---|---|---|---|
| eager | 1、4 | 关、开 | 关 | 4 组，各 15 项通过 |
| lazy v4 | 1、4 | 关、开 | 关、开 | 8 组，各 15 项通过 |

同一份静态探针用于原生和 guest。主代理使用独立的 `supervisor.py`：严格编译、实际探针哈希、精确用例集合、唯一完成标记、退出码、超时和正常清理都参与判定。重复用例、缺项、坏 JSON、清理失败、超时和哈希错误六个反例均被拒绝。作者原始运行器归档作参考，没有用于本轮判定。

原生 ptrace 失败源于旧探针恢复整个寄存器快照，连同 RIP 一起退回此前的执行位置，使第二次执行覆盖第一次采样。修正只恢复被修改的六个通用寄存器，保留当前控制流，并验证第一次采样未被覆盖。本轮 ptrace 全量编辑、恢复和短写检查不再跳过。

需要保留一个实际差异：原生拒绝短 XSTATE SETREGSET 并保持原值，eager 和 lazy guest 均接受短写并保留后缀。本探针验证各自操作的原子性或保留语义，不能由“通过”推导完整的原生 ABI 一致性；此差异正在单独溯源。

独立审查曾报告的 C1 slab 尾部不足和 M2 失败 sigreturn 陈旧状态，已在 `author-evidence/REPORT-CORRECTION.md` 中明确撤回。实际 lazy task_struct 从 2688 增至 2752 字节，分配仍覆盖浮点区；UML 的内联 copy_from_user 会将未复制后缀清零。原始报告同时保留，须结合修正报告阅读，不能把旧指控当成当前缺陷。

仍未覆盖：阻塞线程唤醒后的浮点值断言、fork 子进程写入前的继承值、core dump 的 XSTATE note，以及真实性能收益。模型成功和这轮 guest 正确性均不能代替这些检查。

固定输入：

- eager 内核：`a0bc79d9be2d61b0dbdef8bb34a0fad65f98d8046f196bf21ae663e47c503055`
- lazy 内核：`9f957ded67f8ee74a6178372db7c7acda6aea5581d837603e786f8312a9f836e`
- 实验补丁 0018：`59bd8b361ac69f36c8268aa3e4047c214ac3ecba0724b2cf56518a9a8281d2ca`
- 探针源码：`11e97893f50a6406b7c11a01a3aba129adf03d962f25acf4ce2444c6bb3206c7`
- 实际探针二进制：`7df2e060c549abe2c51a3b1f1d93375f2cbecf43d866ecd4d6d5fced2c7161c7`

`archive-sha256.json` 固定原始日志、冻结源码、修正审查、构建记录和补丁；内核二进制及磁盘留在 Starpub 的 `/root/harddind-rebuild/fp-matrix-owner-v1`。父目录的 `recheck-runtime-candidates.py` 可离线重查归档、全部原始用例、完成标记与清理记录。
