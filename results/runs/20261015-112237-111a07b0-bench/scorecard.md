# Scorecard `20261015-112237-111a07b0-bench`

revision `111a07b0607c` Verify kernel source identity before diagnosing dropped flushes

**Performance** (guest/native, higher is better): key 3/10, important 2/10, other 1/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 3,067,282,120 | 2,953,112,715 ops/s | 0.963 | 0.80 | 0.83–1.07 | -3% | pass |
| key | cpu.multi | 14,855,109,450 | 14,122,438,300 ops/s | 0.951 | 0.80 | 0.93–0.97 | +14% | pass |
| key | mem.bandwidth | 5.33 | 3.83 GB/s | 0.719 | 0.80 | 0.48–0.98 | -9% | fail |
| key | mem.touch | 1.45 | 0.189 GB/s | 0.13 | 0.80 | 0.11–0.15 | -33% | fail |
| key | disk.seq_write | - | -  | - | 0.80 | - | - | fail (guest fsync not durable: pinned UBD FLUS) |
| key | disk.seq_read | 3,358 | 2,893 MB/s | 0.862 | 0.80 | 0.83–0.89 | -24% | pass |
| key | disk.volume_write | - | -  | - | 0.80 | - | - | fail (guest fsync not durable: pinned UBD FLUS) |
| key | net.publish_stream | 3,152 | 144.2 MB/s | 0.0458 | 0.80 | 0.04–0.05 | +44% | fail |
| key | net.egress_stream | 2,700 | 63.1 MB/s | 0.0234 | 0.80 | 0.02–0.02 | -10% | fail |
| key | net.bridge_stream | 3,000 | 1,582 MB/s | 0.527 | 0.80 | 0.47–0.60 | +3% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.67 | 26.4 seconds | 0.139 | 0.50 | 0.14–0.14 | -13% | fail |
| important | app.untar | - | -  | - | 0.50 | - | - | fail (guest fsync not durable: pinned UBD FLUS) |
| important | app.gzip | 0.836 | 0.857 seconds | 0.975 | 0.50 | 0.63–1.27 | +69% | pass |
| important | fs.metadata | 62,135 | 17,884 ops/s | 0.288 | 0.50 | 0.26–0.31 | -10% | fail |
| important | net.publish_rr | 52,669 | 7,752 round trips/s | 0.147 | 0.50 | 0.13–0.17 | +14% | fail |
| important | net.bridge_rr | 50,136 | 8,930 round trips/s | 0.178 | 0.50 | 0.16–0.21 | -18% | fail |
| important | docker.run | ref 0.4 | 1.53 seconds | 0.261 | 0.50 | - | -11% | fail |
| important | docker.exec | ref 0.08 | 0.425 seconds | 0.189 | 0.50 | - | -21% | fail |
| important | docker.build_cached | ref 0.6 | 1.2 seconds | 0.5 | 0.50 | - | -30% | pass |
| important | docker.lifecycle | ref 1.2 | 2.98 seconds | 0.402 | 0.50 | - | -45% | fail |
| other | micro.syscall | 1,900,114 | 212,889 ops/s | 0.112 | 0.20 | 0.10–0.12 | -10% | fail |
| other | micro.forkexec | 3,626 | 399.5 ops/s | 0.11 | 0.20 | 0.10–0.12 | -38% | fail |
| other | micro.mmap | 174,148 | 15,878 ops/s | 0.0912 | 0.20 | 0.08–0.10 | -10% | fail |
| other | micro.clock | 39,424,515 | 242,554 ops/s | 0.0062 | 0.20 | 0.01–0.01 | +17% | fail |
| other | micro.thread | 27,754 | 5,209 ops/s | 0.188 | 0.20 | 0.16–0.22 | -27% | fail |
| other | micro.pipe | 115,052 | 15,818 round trips/s | 0.138 | 0.20 | 0.12–0.15 | +30% | fail |
| other | micro.signal | 369,329 | 97,830 ops/s | 0.265 | 0.20 | 0.23–0.31 | +14% | pass |
