# Scorecard `20261015-114740-61b82666-ci-baseline`

revision `61b82666bfa5` ci: functional phase holds the measure lock shared, bench phase exclusive

**Functional**: 63/130 pass, 67 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 4/13, other 2/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,195,262,710 | 4,060,580,760 ops/s | 0.968 | 0.80 | 0.93–1.04 | - | pass |
| key | cpu.multi | 15,795,950,200 | 16,144,553,400 ops/s | 1.02 | 0.80 | 0.92–1.11 | - | pass |
| key | mem.bandwidth | 5.48 | 5.36 GB/s | 0.978 | 0.80 | 0.97–0.99 | - | pass |
| key | mem.touch | 1.57 | 0.327 GB/s | 0.209 | 0.80 | 0.17–0.23 | - | fail |
| key | disk.seq_write | 116.9 | 612.4 MB/s | 5.24 | 0.80 | 3.68–7.69 | - | pass |
| key | disk.seq_read | 4,409 | 2,422 MB/s | 0.549 | 0.80 | 0.46–1.32 | - | fail |
| key | disk.volume_write | 118.9 | 553.3 MB/s | 4.65 | 0.80 | 3.73–5.36 | - | pass |
| key | net.publish_stream | 3,346 | 269.2 MB/s | 0.0804 | 0.80 | 0.06–0.09 | - | fail |
| key | net.egress_stream | 3,218 | 87.0 MB/s | 0.027 | 0.80 | 0.03–0.03 | - | fail |
| key | net.bridge_stream | 3,196 | 1,473 MB/s | 0.461 | 0.80 | 0.36–0.65 | - | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.56 | 21.6 seconds | 0.164 | 0.50 | 0.15–0.17 | - | fail |
| important | app.untar | 2.95 | 3.23 seconds | 0.915 | 0.50 | 0.89–1.03 | - | pass |
| important | app.gzip | 2.1 | 3.72 seconds | 0.566 | 0.50 | 0.52–0.64 | - | pass |
| important | fs.metadata | 60,585 | 19,133 ops/s | 0.316 | 0.50 | 0.27–0.34 | - | fail |
| important | disk.bind_write | - | -  | - | 0.50 | - | - | fail (RuntimeError: command exited 1: ['chroot) |
| important | disk.bind_read | - | -  | - | 0.50 | - | - | fail (RuntimeError: command exited 1: ['chroot) |
| important | fs.bind_metadata | - | -  | - | 0.50 | - | - | fail (RuntimeError: command exited 1: ['chroot) |
| important | net.publish_rr | 50,426 | 7,495 round trips/s | 0.149 | 0.50 | 0.10–0.16 | - | fail |
| important | net.bridge_rr | 51,265 | 8,863 round trips/s | 0.173 | 0.50 | 0.14–0.23 | - | fail |
| important | docker.run | ref 0.4 | 1.15 seconds | 0.348 | 0.50 | - | - | fail |
| important | docker.exec | ref 0.08 | 0.319 seconds | 0.25 | 0.50 | - | - | fail |
| important | docker.build_cached | ref 0.6 | 0.656 seconds | 0.914 | 0.50 | - | - | pass |
| important | docker.lifecycle | ref 1.2 | 1.49 seconds | 0.803 | 0.50 | - | - | pass |
| other | micro.syscall | 1,867,123 | 241,320 ops/s | 0.129 | 0.20 | 0.12–0.14 | - | fail |
| other | micro.forkexec | 3,070 | 502.9 ops/s | 0.164 | 0.20 | 0.13–0.19 | - | fail |
| other | micro.mmap | 145,765 | 18,367 ops/s | 0.126 | 0.20 | 0.11–0.19 | - | fail |
| other | micro.clock | 38,846,180 | 233,293 ops/s | 0.006 | 0.20 | 0.01–0.01 | - | fail |
| other | micro.thread | 24,505 | 6,577 ops/s | 0.268 | 0.20 | 0.22–0.28 | - | pass |
| other | micro.pipe | 117,070 | 14,976 round trips/s | 0.128 | 0.20 | 0.11–0.16 | - | fail |
| other | micro.signal | 376,243 | 80,223 ops/s | 0.213 | 0.20 | 0.20–0.25 | - | pass |
