# Scorecard `20261016-013258-db04c6fd-ci-net-idle-txpoll`

revision `db04c6fdd44d` Keep port-driver request streams (ports-*.jsonl) with run logs
candidates: net-vector-poll, net-idle-wait; kernel args: uml_vector_tx_poll=1 uml_net_idle_wait=1

**Functional**: 152/154 pass, 2 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 6/10, important 5/13, other 3/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 3,990,298,650 | 3,962,193,240 ops/s | 0.991 | 0.80 | 0.98–1.02 | +6% | pass ~noisy |
| key | cpu.multi | 15,789,618,000 | 15,345,354,200 ops/s | 0.958 | 0.80 | 0.88–1.00 | -2% | pass ~noisy |
| key | mem.bandwidth | 5.25 | 5.1 GB/s | 0.962 | 0.80 | 0.92–1.03 | -6% | pass ~noisy |
| key | mem.touch | 1.57 | 0.643 GB/s | 0.409 | 0.80 | 0.38–0.44 | +27% | fail |
| key | disk.seq_write | 155.8 | 129.8 MB/s | 0.833 | 0.80 | 0.79–0.89 | -7% | pass ~noisy |
| key | disk.seq_read | 3,792 | 2,757 MB/s | 0.78 | 0.80 | 0.64–0.79 | -17% | fail |
| key | disk.volume_write | 158.5 | 146.8 MB/s | 0.94 | 0.80 | 0.89–1.11 | -4% | pass |
| key | net.publish_stream | 1,155 | 1,150 MB/s | 1.03 | 0.80 | 0.78–1.06 | +2% | pass |
| key | net.egress_stream | 3,537 | 955.1 MB/s | 0.278 | 0.80 | 0.25–0.32 | +25% | fail |
| key | net.bridge_stream | 3,435 | 1,848 MB/s | 0.516 | 0.80 | 0.34–0.56 | +61% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.11 | 17.3 seconds | 0.181 | 0.50 | 0.18–0.20 | -28% | fail |
| important | app.untar | 2.1 | 3.66 seconds | 0.611 | 0.50 | 0.54–0.81 | +62% | pass ~noisy |
| important | app.gzip | 2.06 | 3.22 seconds | 0.612 | 0.50 | 0.54–0.65 | -1% | pass |
| important | fs.metadata | 58,482 | 20,260 ops/s | 0.352 | 0.50 | 0.32–0.38 | +69% | fail |
| important | disk.bind_write | 152.7 | 145.1 MB/s | 0.936 | 0.50 | 0.80–1.02 | +1% | pass |
| important | disk.bind_read | 3,390 | 1,592 MB/s | 0.486 | 0.50 | 0.40–0.52 | +27% | fail |
| important | fs.bind_metadata | 57,162 | 1,819 ops/s | 0.0316 | 0.50 | 0.03–0.04 | +216% | fail |
| important | net.publish_rr | 24,224 | 9,081 round trips/s | 0.38 | 0.50 | 0.35–0.39 | +47% | fail |
| important | net.bridge_rr | 51,273 | 9,304 round trips/s | 0.18 | 0.50 | 0.18–0.24 | +7% | fail |
| important | docker.run | ref 0.4 | 1.37 seconds | 0.293 | 0.50 | - | +6% | fail ~noisy |
| important | docker.exec | ref 0.08 | 0.321 seconds | 0.249 | 0.50 | - | -11% | fail |
| important | docker.build_cached | ref 0.6 | 0.815 seconds | 0.736 | 0.50 | - | -8% | pass |
| important | docker.lifecycle | ref 1.2 | 1.38 seconds | 0.869 | 0.50 | - | +28% | pass |
| other | micro.syscall | 1,978,428 | 240,535 ops/s | 0.122 | 0.20 | 0.11–0.13 | +1% | fail |
| other | micro.forkexec | 3,998 | 570.4 ops/s | 0.145 | 0.20 | 0.12–0.19 | +50% | fail |
| other | micro.mmap | 183,262 | 22,082 ops/s | 0.112 | 0.20 | 0.08–0.14 | +103% | fail |
| other | micro.clock | 39,407,494 | 28,514,414 ops/s | 0.705 | 0.20 | 0.70–0.74 | -17% | pass |
| other | micro.thread | 26,121 | 8,112 ops/s | 0.297 | 0.20 | 0.25–0.35 | +36% | pass ~noisy |
| other | micro.pipe | 125,227 | 18,612 round trips/s | 0.15 | 0.20 | 0.13–0.19 | +33% | fail |
| other | micro.signal | 414,425 | 93,499 ops/s | 0.224 | 0.20 | 0.20–0.33 | -4% | pass ~noisy |
