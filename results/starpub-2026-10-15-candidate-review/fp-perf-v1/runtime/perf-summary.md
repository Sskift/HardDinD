# perf summary (DESCRIPTIVE: no threshold, no verdict)

Median seconds of 5 timed repetitions; warmups excluded. Native brackets and lazy0 control are never pooled. Cells with parser/runtime/cleanup errors are EXCLUDED from ratios (see perf-summary.json excluded_cells).

| cell | corebench/syscall | corebench/signals | corebench/threads | corebench/processes | dispatchbench/thread_syscalls |
|---|---|---|---|---|---|
| native-pre | 0.083602 | 0.243434 | 0.750776 | 0.782543 | 0.017840 |
| eager-a | 1.643565 | 0.255385 | 0.387993 | 0.381083 | 0.377776 |
| lazy1-a | 2.181103 | 0.275852 | 0.376850 | 0.374884 | 0.512123 |
| lazy1-b | 2.159831 | 0.253687 | 0.364360 | 0.357176 | 0.424292 |
| eager-b | 1.394319 | 0.253471 | 0.355228 | 0.363419 | 0.342418 |
| lazy0-control | 1.485135 | 0.258215 | 0.421851 | 0.401256 | 0.340365 |
| native-post | 0.074781 | 0.229714 | 0.754765 | 0.405201 | 0.028853 |

## Ratios (median/median)

Full lazy1 x eager cross-products, each guest cell vs EACH native bracket separately, and the unpooled lazy0 control; excluded cells listed: perf-summary.json

