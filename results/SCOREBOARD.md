# Scorecard `20261015-214804-61ab7d1e-ci-faultaround2`

revision `61ab7d1efa82` Merge codex/fault: fault-around only after two adjacent ascending write faults (sparse 384 MiB heap touched every 32 pages: resident 384 -> 12 MiB)
candidates: anon-faultaround, core-faultaround-edges; kernel args: uml_faultaround=5 uml_faultaround_edges=1

**Functional**: 144/146 pass, 2 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 6/10, important 5/13, other 3/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,135,145,350 | 4,100,769,070 ops/s | 0.995 | 0.80 | 0.65–1.03 | +2% | pass |
| key | cpu.multi | 14,164,201,100 | 14,003,159,900 ops/s | 0.979 | 0.80 | 0.91–1.00 | +6% | pass ~noisy |
| key | mem.bandwidth | 4.61 | 4.24 GB/s | 1.02 | 0.80 | 0.77–1.10 | +5% | pass ~noisy |
| key | mem.touch | 1.23 | 0.605 GB/s | 0.493 | 0.80 | 0.31–0.53 | +488% | fail ~noisy |
| key | disk.seq_write | 123.4 | 134.0 MB/s | 1.1 | 0.80 | 0.70–1.24 | +18% | pass ~noisy |
| key | disk.seq_read | 3,758 | 2,983 MB/s | 0.754 | 0.80 | 0.64–0.79 | +3% | fail |
| key | disk.volume_write | 157.1 | 136.9 MB/s | 0.882 | 0.80 | 0.66–1.14 | +28% | pass ~noisy |
| key | net.publish_stream | 1,194 | 948.9 MB/s | 0.818 | 0.80 | 0.66–0.98 | +14% | pass ~noisy |
| key | net.egress_stream | 2,027 | 581.2 MB/s | 0.236 | 0.80 | 0.18–0.34 | +25% | fail ~noisy |
| key | net.bridge_stream | 3,266 | 1,553 MB/s | 0.461 | 0.80 | 0.42–0.84 | +15% | fail ~noisy |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.36 | 18.4 seconds | 0.186 | 0.50 | 0.17–0.24 | -11% | fail ~noisy |
| important | app.untar | 2.15 | 4.15 seconds | 0.519 | 0.50 | 0.45–1.09 | +24% | pass ~noisy |
| important | app.gzip | 2.02 | 3.49 seconds | 0.583 | 0.50 | 0.47–0.62 | -5% | pass ~noisy |
| important | fs.metadata | 58,803 | 19,523 ops/s | 0.319 | 0.50 | 0.31–0.35 | +47% | fail ~noisy |
| important | disk.bind_write | 162.2 | 142.3 MB/s | 0.789 | 0.50 | 0.71–0.98 | -10% | pass |
| important | disk.bind_read | 3,536 | 1,317 MB/s | 0.402 | 0.50 | 0.22–0.48 | +2% | fail |
| important | fs.bind_metadata | 57,187 | 644.5 ops/s | 0.0124 | 0.50 | 0.01–0.01 | -23% | fail ~noisy |
| important | net.publish_rr | 22,056 | 5,080 round trips/s | 0.251 | 0.50 | 0.13–0.32 | -1% | fail ~noisy |
| important | net.bridge_rr | 50,728 | 7,747 round trips/s | 0.153 | 0.50 | 0.13–0.22 | +7% | fail ~noisy |
| important | docker.run | ref 0.4 | 1.94 seconds | 0.206 | 0.50 | - | +9% | fail |
| important | docker.exec | ref 0.08 | 0.314 seconds | 0.255 | 0.50 | - | +40% | fail |
| important | docker.build_cached | ref 0.6 | 1.02 seconds | 0.587 | 0.50 | - | +14% | pass ~noisy |
| important | docker.lifecycle | ref 1.2 | 2.25 seconds | 0.533 | 0.50 | - | -1% | pass ~noisy |
| other | micro.syscall | 1,965,374 | 238,055 ops/s | 0.123 | 0.20 | 0.11–0.14 | +18% | fail ~noisy |
| other | micro.forkexec | 3,914 | 11.9 ops/s | 0.0029 | 0.20 | 0.00–0.00 | +12% | fail ~noisy |
| other | micro.mmap | 190,842 | 24,397 ops/s | 0.137 | 0.20 | 0.11–0.15 | +147% | fail ~noisy |
| other | micro.clock | 35,919,058 | 25,701,133 ops/s | 0.708 | 0.20 | 0.64–0.84 | -15% | pass |
| other | micro.thread | 25,453 | 5,630 ops/s | 0.216 | 0.20 | 0.19–0.27 | +2% | pass ~noisy |
| other | micro.pipe | 117,384 | 13,853 round trips/s | 0.133 | 0.20 | 0.11–0.20 | +15% | fail ~noisy |
| other | micro.signal | 402,758 | 89,121 ops/s | 0.221 | 0.20 | 0.21–0.24 | +5% | pass ~noisy |
