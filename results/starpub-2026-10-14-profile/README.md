# 执行开销定位：2026-10-14

本轮只增加诊断程序、追踪和分析，不改变运行内核。确认了系统调用拦截、首次缺页、权限更新和执行进程重建的具体路径；尚未逐函数分摊全部 CPU 时间，也没有实现下面提出的优化。

## 测量方法

- Starpub 当前无特权外层容器，固定宿主 CPU 0、1、2、3，guest 4 vCPU、2 GiB。
- 内核为上一轮的 `memfd-kernel-reviewed`，SHA256 `e85dfb9e289557753e4eb797c95482741a90f8ae4eb71afcd56ad3a09ed04c19`，启用并行执行和映射代理缓存。
- 原生和 guest 使用同一个静态程序，SHA256 `7f6b6dd37da5ccb75eba34ac28f24802f5a90221988cbc6d11c031b45fd3555f`。本测试拆分路径，与之前 `dispatchbench` 的测试体不同，不能把两轮差异当作版本提速。
- 计时顺序：原生、简化 SIGSYS、spin=0、256、4096、4096、256、0、简化 SIGSYS、原生。每次每项 3 个样本，共每模式每项 6 个样本。
- 另起一次 `strace`，使用缩小的循环数统计路径。**追踪耗时不进入任何性能比较**。分析器扣除被 SIGSYS 拦截的 guest 调用，并避免重复计算 `resumed` 行和串口输出的标记。
- 10 次未追踪运行、1 次追踪运行均完整结束，检查通过，独立 COW 层已删除，无遗留进程。每轮保存内核、程序、基础磁盘哈希和运行前后 cgroup 状态。
- CPU 没有独占。10 次计时运行的 cgroup `nr_throttled`、`throttled_usec` 增量均为 0；这排除了本轮的 quota 节流，不能排除其他进程争用、调度和频率波动。

完整样本和 min/max 见 [analysis.json](analysis.json)，原始日志及压缩追踪保留在本目录。

本机与 Starpub 的 `make check` 均通过 10 项检查。源码哈希、11 个实验进程组的最终清理检查和原有 `/root/udock` 健康状态见 [profile-provenance.json](profile-provenance.json)；远端校验输出见 [profile-validation.log](profile-validation.log)。

## 1. 系统调用：信号拦截与跨进程交接

10 万次真实 `syscall(SYS_getpid)`，单次中位耗时：

| 路径 | 微秒/次 |
| --- | ---: |
| 原生 | 0.706 |
| 仅 SIGSYS 处理、没有 UML 或进程交接 | 2.099 |
| 当前 UML，spin=256 | 3.998 |
| 当前 UML，spin=0 | 13.148 |

简化 SIGSYS 对照只在本进程的信号处理函数里设置返回值，用来测量信号路径成本；它不提供容器语义，不是可用后端，也不是给产品加入 getpid 缓存。它的耗时不能直接从 UML 耗时中相减，宣称得到精确的组件占比。

代码路径：`arch/um/kernel/skas/stub_exe.c` 安装过滤器 → `stub.c:stub_signal_interrupt` → 共享状态字及 futex 交接 → `os-Linux/skas/process.c:userspace` → guest 内核系统调用 → 设置返回状态 → `rt_sigreturn`。`arch/x86/um/os-Linux/mcontext.c:get_stub_state/set_stub_state` 每次还复制通用寄存器和完整浮点扩展状态。浮点复制的独立耗时尚未测量，不能称为已确认的最大热点。

本轮 200 次 getpid 追踪确实观察到 200 个 getpid SIGSYS。SIGSYS 和信号返回属于这条入口路径的实际工作；[Linux seccomp 文档](https://docs.kernel.org/userspace-api/seccomp_filter.html) 明确说明 `SECCOMP_RET_TRAP` 发送 SIGSYS，原始系统调用不执行。

优化方向：先减少每次交接的工作，再研究保持完整 guest 语义的更低成本入口。仅改 Docker 命令层不会改变这条执行路径。

## 2. 映射：每页缺页与大量信号屏蔽

每轮分配 64 KiB、访问指定页、校验数据并释放；单位为微秒/轮，当前配置 spin=256：

| 测试 | 原生 | 当前 UML |
| --- | ---: | ---: |
| mmap + munmap，不触碰页面 | 2.354 | 16.061 |
| 首次写入 1 页 | 5.918 | 100.280 |
| 首次写入 16 页 | 34.615 | 583.651 |
| 写入 16 页，再改成只读 | 36.932 | 689.812 |

增加 15 个首次触碰页面，使 UML 每轮增加约 483 微秒；原生增加约 29 微秒。主要放大发生在页面建立和权限维护路径。

8 轮首次写入 16 页的追踪中，记录到：

- **128 次 SIGSEGV、128 次宿主 mmap**：每页都通过 runner → UML 缺页处理 → 映射代理 → runner 的路径建立宿主映射。
- **3,782 次宿主 `rt_sigprocmask`**，同一追踪的空阶段为 16 次。追踪会增加计时器活动，该数字是被追踪运行的计数，不能当作未追踪时的固定每页次数。
- 单个缺页片段中可直接看到多组 `SIG_BLOCK [RT_2]` / `SIG_UNBLOCK [RT_2]`。原始追踪解压后约第 111,175–111,310 行保留了一个完整缺页及释放过程。

源头是 `os-Linux/signal.c:__block_signals/__unblock_signals` 调用 `os-Linux/smp.c:__local_ipi_set`；后者每次真实开关都调用宿主 `sigprocmask`。普通 Linux 中代价很低的中断屏蔽操作，在 UML 内存分配、页表锁和同步流程中变成多次宿主系统调用。已有空 TLB 同步快路径只解决无映射工作的情况。

另用未追踪的宿主程序测了 6 组、每组 10 万次相同的屏蔽/解除屏蔽：**一对操作中位数 1.549 微秒**。这是判断优化优先级的独立成本参考；不能乘以追踪计数便声称得到真实运行的耗时占比。

优先优化：合并不必要的中断屏蔽边界；进一步验证 IPI 在软件中登记 pending、恢复中断时处理的方案。不能直接删掉信号屏蔽；必须保证并发 IPI 不丢失、不在禁止中断的临界区重入，并回归调度、COW、TLB 撤销和页表释放。

## 3. mprotect：只改权限也在重建映射

`protect_pair` 预先建立 64 KiB 映射，循环执行只读→读写，不重新分配内存。每对操作原生 **5.025 微秒**，UML **125.535 微秒**。

8 对权限操作的追踪对应 16 个 guest mprotect，却产生 **128 个实际宿主 mmap**。原因已定位到 `kernel/tlb.c:update_pte_range`：只要 PTE 需要同步且 present，就统一调用 `ops->mmap`；`stub.c:syscall_handler` 随后执行 `MAP_SHARED | MAP_FIXED`。现有合并要求物理偏移连续，不能把所有权限更新合并成一个操作。

优化方向：增加独立的保护权限操作；在确认宿主映射仍是同一物理页、只改变权限时，使用宿主 mprotect，并按地址和权限合并连续范围。COW 换页、撤销映射以及未建立映射仍保留原路径；撤销写权限必须在返回前同步完成。这比无条件扩大每次缺页的预取范围更直接，也更容易单独验证。

## 4. fork/exec：代理缓存没有消除执行进程重建

| 测试 | 原生，微秒/次 | UML spin=256，微秒/次 |
| --- | ---: | ---: |
| fork + 子进程退出 + wait | 94.890 | 1,021.002 |
| fork + exec 静态程序 + wait | 253.404 | 3,016.714 |

追踪扣除 guest 自己发起但被拦截的调用后：

| 4 次循环 | 实际宿主 clone | SIGSEGV | 实际宿主 mmap |
| --- | ---: | ---: | ---: |
| fork | 4 | 32 | 118 |
| fork + exec | 8 | 136 | 261 |

两个阶段均没有实际宿主 execveat，说明代理缓存确实避免了重新加载 stub ELF；但 fork 和 exec 的新地址空间分别仍需创建 runner。随后程序代码、栈和 libc 初始化又触发多次缺页。根因包含进程重建和映射放大，不能全部归结为 clone 一项。

优化方向：先让映射和中断路径变快，再验证同时复用干净代理及 runner 的地址空间生命周期，或者在满足独占条件时复用 exec 执行上下文。必须重置 TLS、信号帧、FD、旧映射，并覆盖多线程 exec、并发 fork 与进程退出；不能拿掉隔离检查换速度。

## 5. 轮询需要按交接类型调整

| 单项微秒/次或轮 | spin=0 | spin=256 | spin=4096 |
| --- | ---: | ---: | ---: |
| getpid | 13.148 | 3.998 | 4.147 |
| 16 页映射 + 只读 | 903.578 | 689.812 | 510.484 |
| fork | 1,295.293 | 1,021.002 | 1,809.402 |
| fork + exec | 3,427.430 | 3,016.714 | 3,610.118 |

spin=0 两轮各记录约 67 万、69 万次宿主自愿上下文切换；256 为约 5.3 万、2.2 万；4096 为约 8 千、7 千。该统计包含完整 guest 启停，不能精确分配到某个测试。

更长轮询降低睡眠交接，却在本轮增加 fork/exec 耗时，且 4096 的整套 guest CPU 时间两次分别为 5.968 和 3.491 秒，波动明显。下一步适合区分快速 runner 交接与映射/启动等较长代理操作，测量后再选择有上限的自适应等待；本轮没有理由统一把默认值改为 4096。

## 优化顺序与未解问题

1. 减少内存路径的信号屏蔽系统调用；先合并边界，再验证软件 IPI pending 机制。
2. 分离权限变更与物理页重映射，合并可合并的保护更新。
3. 在前两项基础上优化干净地址空间和 runner 的重用，降低 fork/exec 生命周期成本。
4. 分类型测量交接，优化轮询策略，并单独测量寄存器/浮点状态复制。

这些都是保持功能的候选优化，还没有实测它们能减少多少时间。当前入口保留 SIGSYS 和跨进程交接开销，不能承诺完成以上项目就达到完整原生性能。GPU 缺少设备 ABI 后端，属于独立的功能缺口；本轮没有 GPU 验收。上一轮偶发的宿主进程丢失崩溃也没有因本轮诊断成功而被判定修复。

## 复现

已有 `dispatch-root` 和上一轮内核时：

```sh
bash scripts/make-profile-root.sh /root/harddind-rebuild
python3 scripts/profile-dispatch.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/memfd-kernel-reviewed \
  --mode guest --spin 256 --name profile-time-3-guest-256
# 按上述顺序运行其余模式；trace 模式只追踪缩小后的循环。
python3 scripts/profile-dispatch.py /root/harddind-rebuild \
  --kernel /root/harddind-rebuild/memfd-kernel-reviewed \
  --mode trace --name profile-trace-256
gcc -O2 -Wall -Wextra -Werror -static src/irqcost.c -o /root/harddind-rebuild/irqcost
taskset -c 0,1,2,3 /root/harddind-rebuild/irqcost > /root/harddind-rebuild/results/profile-irqcost.jsonl
# 收集日志、JSON、jsonl，gzip -n 压缩 strace 后：
python3 scripts/analyze-profile.py results/starpub-2026-10-14-profile
make check
```

每次运行拒绝覆盖已有证据；实验期间不要同时编译或运行其他基准。计时报告中的 `command` 保存完整参数。分析器要求完整的 10 次交错运行、相同哈希与 CPU 集合，缺失或失败均拒绝生成完成结论。
