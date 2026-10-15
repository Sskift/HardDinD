# Scorecard `20261015-111256-33daff24-bench`

revision `33daff2433db` Amortize benchmark clocks and isolate metric budgets

**Performance** (guest/native, higher is better): key 3/10, important 3/10, other 2/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,167,558,065 | 4,141,306,835 ops/s | 0.994 | 0.80 | 0.98–1.01 | - | pass |
| key | cpu.multi | 16,078,084,950 | 13,463,982,950 ops/s | 0.837 | 0.80 | 0.78–0.89 | - | pass |
| key | mem.bandwidth | 5.46 | 4.3 GB/s | 0.787 | 0.80 | 0.70–0.88 | - | fail |
| key | mem.touch | 1.48 | 0.288 GB/s | 0.195 | 0.80 | 0.17–0.22 | - | fail |
| key | disk.seq_write | - | -  | - | 0.80 | - | - | fail (guest fsync not durable: UBD FLUSH desc_) |
| key | disk.seq_read | 2,915 | 3,290 MB/s | 1.13 | 0.80 | 0.70–2.26 | - | pass |
| key | disk.volume_write | - | -  | - | 0.80 | - | - | fail (guest fsync not durable: UBD FLUSH desc_) |
| key | net.publish_stream | 3,047 | 96.8 MB/s | 0.0318 | 0.80 | 0.01–0.05 | -58% | fail |
| key | net.egress_stream | 3,362 | 87.1 MB/s | 0.0259 | 0.80 | 0.03–0.03 | -11% | fail |
| key | net.bridge_stream | 3,291 | 1,692 MB/s | 0.514 | 0.80 | 0.46–0.57 | +54% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 4.08 | 25.5 seconds | 0.16 | 0.50 | 0.14–0.18 | - | fail |
| important | app.untar | - | -  | - | 0.50 | - | - | fail (guest fsync not durable: UBD FLUSH desc_) |
| important | app.gzip | 0.516 | 0.893 seconds | 0.578 | 0.50 | 0.55–0.60 | - | pass |
| important | fs.metadata | 54,060 | 17,349 ops/s | 0.321 | 0.50 | 0.24–0.44 | - | fail |
| important | net.publish_rr | 50,289 | 6,473 round trips/s | 0.129 | 0.50 | 0.11–0.14 | +52% | fail |
| important | net.bridge_rr | 52,697 | 11,506 round trips/s | 0.218 | 0.50 | 0.20–0.23 | +58% | fail |
| important | docker.run | ref 0.4 | 1.37 seconds | 0.292 | 0.50 | - | - | fail |
| important | docker.exec | ref 0.08 | 0.337 seconds | 0.238 | 0.50 | - | - | fail |
| important | docker.build_cached | ref 0.6 | 0.843 seconds | 0.712 | 0.50 | - | - | pass |
| important | docker.lifecycle | ref 1.2 | 1.64 seconds | 0.732 | 0.50 | - | - | pass |
| other | micro.syscall | 1,948,141 | 243,107 ops/s | 0.125 | 0.20 | 0.12–0.13 | - | fail |
| other | micro.forkexec | 2,334 | 414.3 ops/s | 0.177 | 0.20 | 0.12–0.30 | - | fail |
| other | micro.mmap | 185,814 | 18,783 ops/s | 0.101 | 0.20 | 0.08–0.12 | - | fail |
| other | micro.clock | 39,299,747 | 209,419 ops/s | 0.0053 | 0.20 | 0.00–0.01 | - | fail |
| other | micro.thread | 28,317 | 7,311 ops/s | 0.258 | 0.20 | 0.20–0.32 | - | pass |
| other | micro.pipe | 125,795 | 13,348 round trips/s | 0.106 | 0.20 | 0.09–0.13 | - | fail |
| other | micro.signal | 412,310 | 95,832 ops/s | 0.232 | 0.20 | 0.23–0.24 | - | pass |
