# Uninstrumented GPU run fails before performance comparison

The same mapping-v14 kernel, rootfs, CUDA binary, driver libraries, devices,
four CPUs and memfd settings were run without LD_PRELOAD or strace. Seven
fresh native processes (two warmups, five planned samples) each verified all
four GPUs. The first internal Docker process then failed at
`cuCtxCreate_v2(&ctx,0,dev)` with **CUDA error 304**, exit 1 and no result rows.
The experiment stopped; native-after was not run and no ratios were produced.

This means [v14's four-card success](../gpu-mapping-v14/README.md) is currently
conditional on the traced run. It does not establish stable ordinary CUDA
execution. The only tracing differences are removal of the preload logger
and strace; container names, image tag and `--rm` also differ. The first run
alone cannot isolate which difference matters. A separate repeated 2×2
instrumentation matrix is required; timings from this failed experiment
cannot be labeled GPU performance acceptance.

The guest stopped normally, inputs/libraries stayed unchanged and 14 recorded
command/runtime groups were empty. Exact commands, all native outputs, the
failed stderr and runtime state are retained. `python3 recheck.py` verifies
both the native successes and the absence of internal/performance results.
