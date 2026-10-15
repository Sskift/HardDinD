# Scorecard `20261015-131801-ddd53ed9-ci-fsync-fixed`

revision `ddd53ed91cb1` Merge branch 'codex/bench'

**Functional**: 126/130 pass, 4 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 5/13, other 2/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,159,001,310 | 3,964,720,270 ops/s | 0.953 | 0.80 | 0.94–0.98 | +4% | pass |
| key | cpu.multi | 15,693,578,000 | 16,102,575,100 ops/s | 1.03 | 0.80 | 0.99–1.07 | +38% | pass |
| key | mem.bandwidth | 5.4 | 5.38 GB/s | 0.996 | 0.80 | 0.99–1.02 | -28% | pass |
| key | mem.touch | 1.58 | 0.318 GB/s | 0.201 | 0.80 | 0.15–0.21 | +22% | fail |
| key | disk.seq_write | 160.7 | 138.1 MB/s | 0.859 | 0.80 | 0.68–1.07 | -79% | pass |
| key | disk.seq_read | 4,484 | 2,578 MB/s | 0.575 | 0.80 | 0.57–0.75 | -30% | fail |
| key | disk.volume_write | 139.8 | 126.7 MB/s | 0.906 | 0.80 | 0.73–1.15 | -84% | pass |
| key | net.publish_stream | 3,209 | 243.0 MB/s | 0.0757 | 0.80 | 0.07–0.08 | +5% | fail |
| key | net.egress_stream | 3,200 | 79.9 MB/s | 0.025 | 0.80 | 0.02–0.03 | -15% | fail |
| key | net.bridge_stream | 3,160 | 1,590 MB/s | 0.503 | 0.80 | 0.42–0.53 | +4% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 5.06 | 25.2 seconds | 0.201 | 0.50 | 0.14–0.20 | +28% | fail |
| important | app.untar | 2.05 | 3.8 seconds | 0.541 | 0.50 | 0.45–0.85 | -33% | pass |
| important | app.gzip | 2.07 | 3.36 seconds | 0.617 | 0.50 | 0.60–0.63 | +16% | pass |
| important | fs.metadata | 62,070 | 18,014 ops/s | 0.29 | 0.50 | 0.28–0.32 | -10% | fail |
| important | disk.bind_write | 162.6 | 138.6 MB/s | 0.852 | 0.50 | 0.73–0.95 | - | pass |
| important | disk.bind_read | 3,811 | 1,151 MB/s | 0.302 | 0.50 | 0.12–0.38 | - | fail |
| important | fs.bind_metadata | 58,992 | 988.4 ops/s | 0.0168 | 0.50 | 0.01–0.02 | - | fail |
| important | net.publish_rr | 52,066 | 5,283 round trips/s | 0.102 | 0.50 | 0.02–0.16 | -34% | fail |
| important | net.bridge_rr | 50,467 | 8,498 round trips/s | 0.168 | 0.50 | 0.13–0.23 | +14% | fail |
| important | docker.run | ref 0.4 | 1.7 seconds | 0.235 | 0.50 | - | +9% | fail |
| important | docker.exec | ref 0.08 | 0.335 seconds | 0.238 | 0.50 | - | +40% | fail |
| important | docker.build_cached | ref 0.6 | 1.09 seconds | 0.551 | 0.50 | - | +3% | pass |
| important | docker.lifecycle | ref 1.2 | 2.13 seconds | 0.563 | 0.50 | - | -4% | pass |
| other | micro.syscall | 1,894,393 | 245,753 ops/s | 0.13 | 0.20 | 0.10–0.14 | +3% | fail |
| other | micro.forkexec | 3,449 | 447.9 ops/s | 0.13 | 0.20 | 0.11–0.18 | -2% | fail |
| other | micro.mmap | 182,078 | 16,842 ops/s | 0.0925 | 0.20 | 0.08–0.11 | -26% | fail |
| other | micro.clock | 39,585,070 | 198,996 ops/s | 0.005 | 0.20 | 0.00–0.01 | -18% | fail |
| other | micro.thread | 25,483 | 5,943 ops/s | 0.233 | 0.20 | 0.20–0.29 | +0% | pass |
| other | micro.pipe | 120,246 | 14,396 round trips/s | 0.12 | 0.20 | 0.10–0.15 | +0% | fail |
| other | micro.signal | 399,030 | 100,739 ops/s | 0.253 | 0.20 | 0.24–0.26 | -0% | pass |
