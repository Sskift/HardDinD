# Scorecard `20261016-050109-23d22b41-ci-rseq-irqwork-moby0004`

revision `23d22b41ecf7` docs: rseq fork/exec regression root cause (irq_work_sync waited for RCU) and fix

**Functional**: 158/159 pass, 1 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 6/10, important 5/13, other 4/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,056,006,560 | 3,952,750,050 ops/s | 1 | 0.80 | 0.94–1.04 | +3% | pass |
| key | cpu.multi | 16,119,402,000 | 13,836,588,800 ops/s | 0.921 | 0.80 | 0.83–1.02 | -7% | pass |
| key | mem.bandwidth | 5.41 | 5.21 GB/s | 0.974 | 0.80 | 0.91–1.00 | -2% | pass |
| key | mem.touch | 1.58 | 0.662 GB/s | 0.411 | 0.80 | 0.32–0.45 | -3% | fail |
| key | disk.seq_write | 178.0 | 156.3 MB/s | 0.902 | 0.80 | 0.88–1.03 | +9% | pass ~noisy |
| key | disk.seq_read | 3,520 | 2,724 MB/s | 0.775 | 0.80 | 0.70–0.96 | -2% | fail |
| key | disk.volume_write | 143.3 | 164.1 MB/s | 1.05 | 0.80 | 0.96–1.43 | +7% | pass ~noisy |
| key | net.publish_stream | 1,156 | 1,201 MB/s | 1.02 | 0.80 | 0.84–1.07 | +2% | pass |
| key | net.egress_stream | 3,399 | 1,119 MB/s | 0.332 | 0.80 | 0.27–0.42 | +6% | fail ~noisy |
| key | net.bridge_stream | 3,509 | 1,720 MB/s | 0.494 | 0.80 | 0.48–0.58 | -7% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.51 | 17.1 seconds | 0.203 | 0.50 | 0.19–0.23 | -11% | fail |
| important | app.untar | 2.51 | 3.49 seconds | 0.673 | 0.50 | 0.59–0.87 | +12% | pass ~noisy |
| important | app.gzip | 2.15 | 3.26 seconds | 0.661 | 0.50 | 0.58–0.71 | +4% | pass |
| important | fs.metadata | 59,651 | 20,225 ops/s | 0.339 | 0.50 | 0.29–0.36 | +5% | fail |
| important | disk.bind_write | 154.1 | 134.7 MB/s | 0.88 | 0.50 | 0.80–0.98 | -2% | pass |
| important | disk.bind_read | 3,511 | 1,635 MB/s | 0.492 | 0.50 | 0.34–0.51 | +6% | fail |
| important | fs.bind_metadata | 55,134 | 1,641 ops/s | 0.0311 | 0.50 | 0.03–0.04 | +6% | fail ~noisy |
| important | net.publish_rr | 22,640 | 9,656 round trips/s | 0.426 | 0.50 | 0.38–0.46 | -0% | fail |
| important | net.bridge_rr | 48,582 | 11,068 round trips/s | 0.231 | 0.50 | 0.18–0.28 | +8% | fail ~noisy |
| important | docker.run | ref 0.4 | 1.2 seconds | 0.333 | 0.50 | - | -6% | fail |
| important | docker.exec | ref 0.08 | 0.302 seconds | 0.265 | 0.50 | - | -2% | fail |
| important | docker.build_cached | ref 0.6 | 0.982 seconds | 0.611 | 0.50 | - | -18% | pass ~noisy |
| important | docker.lifecycle | ref 1.2 | 1.5 seconds | 0.801 | 0.50 | - | +5% | pass |
| other | micro.syscall | 1,795,113 | 212,885 ops/s | 0.126 | 0.20 | 0.06–0.13 | +0% | fail |
| other | micro.forkexec | 3,410 | 399.2 ops/s | 0.121 | 0.20 | 0.12–0.17 | -16% | fail ~noisy |
| other | micro.mmap | 181,887 | 20,862 ops/s | 0.122 | 0.20 | 0.10–0.13 | +34% | fail |
| other | micro.clock | 38,753,630 | 24,543,909 ops/s | 0.665 | 0.20 | 0.53–0.68 | -8% | pass |
| other | micro.thread | 24,413 | 6,764 ops/s | 0.277 | 0.20 | 0.25–0.30 | -1% | pass ~noisy |
| other | micro.pipe | 91,112 | 18,313 round trips/s | 0.216 | 0.20 | 0.17–0.26 | +9% | pass ~noisy |
| other | micro.signal | 365,044 | 74,014 ops/s | 0.202 | 0.20 | 0.17–0.25 | +4% | pass ~noisy |
