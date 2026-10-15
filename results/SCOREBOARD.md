# Scorecard `20261015-213510-8c64e357-ci-clock-rseq-net3`

revision `8c64e357450d` Merge codex/vdso round 3: guest clock disciplined against the host; enable rseq

**Functional**: 136/139 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 4/10, important 4/13, other 3/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,140,573,000 | 4,037,120,150 ops/s | 0.972 | 0.80 | 0.92–0.99 | -2% | pass |
| key | cpu.multi | 15,819,388,700 | 14,610,172,400 ops/s | 0.924 | 0.80 | 0.83–1.09 | -6% | pass ~noisy |
| key | mem.bandwidth | 4.88 | 4.95 GB/s | 0.977 | 0.80 | 0.95–1.12 | +0% | pass ~noisy |
| key | mem.touch | 1.35 | 0.112 GB/s | 0.0839 | 0.80 | 0.06–0.10 | -1% | fail ~noisy |
| key | disk.seq_write | 133.2 | 116.2 MB/s | 0.927 | 0.80 | 0.82–1.04 | -15% | pass ~noisy |
| key | disk.seq_read | 4,111 | 2,641 MB/s | 0.73 | 0.80 | 0.58–1.08 | -6% | fail ~noisy |
| key | disk.volume_write | 148.6 | 116.4 MB/s | 0.688 | 0.80 | 0.63–1.00 | -28% | fail ~noisy |
| key | net.publish_stream | 1,125 | 838.7 MB/s | 0.718 | 0.80 | 0.51–0.86 | -5% | fail ~noisy |
| key | net.egress_stream | 3,431 | 622.3 MB/s | 0.189 | 0.80 | 0.14–0.29 | -12% | fail |
| key | net.bridge_stream | 3,388 | 1,349 MB/s | 0.402 | 0.80 | 0.37–0.44 | +1% | fail ~noisy |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.74 | 18.3 seconds | 0.21 | 0.50 | 0.19–0.22 | -4% | fail |
| important | app.untar | 2.28 | 5.82 seconds | 0.418 | 0.50 | 0.34–0.53 | -72% | fail ~noisy |
| important | app.gzip | 2.09 | 3.37 seconds | 0.614 | 0.50 | 0.58–0.64 | -1% | pass |
| important | fs.metadata | 67,100 | 14,564 ops/s | 0.217 | 0.50 | 0.18–0.23 | -14% | fail ~noisy |
| important | disk.bind_write | 163.9 | 141.2 MB/s | 0.874 | 0.50 | 0.78–0.93 | +18% | pass ~noisy |
| important | disk.bind_read | 3,544 | 1,231 MB/s | 0.393 | 0.50 | 0.14–0.45 | -6% | fail |
| important | fs.bind_metadata | 70,129 | 1,111 ops/s | 0.016 | 0.50 | 0.01–0.02 | +0% | fail ~noisy |
| important | net.publish_rr | 20,916 | 5,200 round trips/s | 0.254 | 0.50 | 0.10–0.31 | +103% | fail |
| important | net.bridge_rr | 53,739 | 7,710 round trips/s | 0.143 | 0.50 | 0.13–0.20 | -3% | fail ~noisy |
| important | docker.run | ref 0.4 | 2.11 seconds | 0.19 | 0.50 | - | +62% | fail |
| important | docker.exec | ref 0.08 | 0.44 seconds | 0.182 | 0.50 | - | +3% | fail |
| important | docker.build_cached | ref 0.6 | 1.17 seconds | 0.513 | 0.50 | - | +524% | pass ~noisy |
| important | docker.lifecycle | ref 1.2 | 2.24 seconds | 0.537 | 0.50 | - | +188% | pass |
| other | micro.syscall | 2,096,891 | 208,862 ops/s | 0.104 | 0.20 | 0.10–0.12 | -13% | fail ~noisy |
| other | micro.forkexec | 4,084 | 11.2 ops/s | 0.0026 | 0.20 | 0.00–0.00 | -98% | fail ~noisy |
| other | micro.mmap | 187,241 | 10,297 ops/s | 0.0552 | 0.20 | 0.05–0.06 | +4% | fail |
| other | micro.clock | 38,497,992 | 32,307,107 ops/s | 0.828 | 0.20 | 0.72–1.11 | -20% | pass ~noisy |
| other | micro.thread | 26,291 | 4,993 ops/s | 0.213 | 0.20 | 0.17–0.23 | -2% | pass |
| other | micro.pipe | 120,425 | 14,725 round trips/s | 0.116 | 0.20 | 0.11–0.13 | -1% | fail |
| other | micro.signal | 403,460 | 83,260 ops/s | 0.211 | 0.20 | 0.19–0.24 | +19% | pass ~noisy |
