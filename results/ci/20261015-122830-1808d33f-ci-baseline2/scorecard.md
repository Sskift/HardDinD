# Scorecard `20261015-122830-1808d33f-ci-baseline2`

revision `1808d33f6690` Shared file/lock checks use ctx.share (broken by the share rename)

**Functional**: 127/130 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 4/13, other 2/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 3,948,568,910 | 3,624,152,060 ops/s | 0.918 | 0.80 | 0.86–0.95 | -5% | pass |
| key | cpu.multi | 11,986,209,400 | 8,943,303,760 ops/s | 0.746 | 0.80 | 0.28–1.02 | -27% | fail |
| key | mem.bandwidth | 3.85 | 5.3 GB/s | 1.38 | 0.80 | 0.78–1.38 | +41% | pass |
| key | mem.touch | 1.4 | 0.229 GB/s | 0.164 | 0.80 | 0.10–0.18 | -21% | fail |
| key | disk.seq_write | 134.5 | 543.3 MB/s | 4.04 | 0.80 | 3.28–4.41 | -23% | pass |
| key | disk.seq_read | 4,105 | 3,369 MB/s | 0.821 | 0.80 | 0.77–0.88 | +49% | pass |
| key | disk.volume_write | 109.9 | 636.8 MB/s | 5.79 | 0.80 | 4.91–8.62 | +24% | pass |
| key | net.publish_stream | 3,330 | 240.6 MB/s | 0.0723 | 0.80 | 0.07–0.08 | -10% | fail |
| key | net.egress_stream | 3,267 | 95.7 MB/s | 0.0293 | 0.80 | 0.03–0.03 | +9% | fail |
| key | net.bridge_stream | 3,372 | 1,639 MB/s | 0.486 | 0.80 | 0.44–0.50 | +5% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.52 | 22.5 seconds | 0.157 | 0.50 | 0.14–0.18 | -5% | fail |
| important | app.untar | 3.17 | 3.91 seconds | 0.811 | 0.50 | 0.65–0.94 | -11% | pass |
| important | app.gzip | 2.13 | 4 seconds | 0.533 | 0.50 | 0.49–0.68 | -6% | pass |
| important | fs.metadata | 51,054 | 16,383 ops/s | 0.321 | 0.50 | 0.22–0.36 | +2% | fail |
| important | disk.bind_write | - | -  | - | 0.50 | - | - | fail (RuntimeError: command exited 1: ['chroot) |
| important | disk.bind_read | - | -  | - | 0.50 | - | - | fail (RuntimeError: command exited 1: ['chroot) |
| important | fs.bind_metadata | - | -  | - | 0.50 | - | - | fail (RuntimeError: command exited 1: ['chroot) |
| important | net.publish_rr | 49,319 | 7,636 round trips/s | 0.155 | 0.50 | 0.11–0.17 | +4% | fail |
| important | net.bridge_rr | 45,421 | 6,711 round trips/s | 0.148 | 0.50 | 0.13–0.17 | -15% | fail |
| important | docker.run | ref 0.4 | 1.85 seconds | 0.216 | 0.50 | - | -38% | fail |
| important | docker.exec | ref 0.08 | 0.471 seconds | 0.17 | 0.50 | - | -32% | fail |
| important | docker.build_cached | ref 0.6 | 1.12 seconds | 0.535 | 0.50 | - | -42% | pass |
| important | docker.lifecycle | ref 1.2 | 2.04 seconds | 0.588 | 0.50 | - | -27% | pass |
| other | micro.syscall | 1,849,535 | 232,796 ops/s | 0.126 | 0.20 | 0.11–0.14 | -3% | fail |
| other | micro.forkexec | 3,480 | 459.7 ops/s | 0.132 | 0.20 | 0.10–0.15 | -19% | fail |
| other | micro.mmap | 183,957 | 22,973 ops/s | 0.125 | 0.20 | 0.10–0.14 | -1% | fail |
| other | micro.clock | 37,915,515 | 230,384 ops/s | 0.0061 | 0.20 | 0.01–0.01 | +2% | fail |
| other | micro.thread | 22,310 | 5,184 ops/s | 0.232 | 0.20 | 0.17–0.34 | -13% | pass |
| other | micro.pipe | 122,794 | 14,633 round trips/s | 0.119 | 0.20 | 0.09–0.16 | -7% | fail |
| other | micro.signal | 373,505 | 94,648 ops/s | 0.253 | 0.20 | 0.25–0.27 | +19% | pass |
