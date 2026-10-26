# Scorecard `20261026-054049-05a863d8-ci-runner-exit-fix`

revision `05a863d84a00` Merge commit '576606802ea94f3be4c2d8b8c0e10a838ef44a50' into codex/ops

**Functional**: 172/175 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 4/13, other 3/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,233,287,760 | 4,310,865,040 ops/s | 0.982 | 0.80 | 0.95–1.03 | +4% | pass |
| key | cpu.multi | 17,596,070,500 | 17,434,359,200 ops/s | 0.988 | 0.80 | 0.97–1.01 | +4% | pass |
| key | mem.bandwidth | 5.59 | 5.32 GB/s | 0.952 | 0.80 | 0.94–0.97 | +1% | pass |
| key | mem.touch | 1.91 | 0.39 GB/s | 0.203 | 0.80 | 0.18–0.23 | -71% | fail |
| key | disk.seq_write | 170.8 | 149.9 MB/s | 0.863 | 0.80 | 0.85–0.90 | +2% | pass |
| key | disk.seq_read | 3,664 | 1,926 MB/s | 0.519 | 0.80 | 0.51–0.63 | -31% | fail |
| key | disk.volume_write | 167.5 | 157.0 MB/s | 0.963 | 0.80 | 0.90–1.04 | +19% | pass |
| key | net.publish_stream | 3,439 | 1,368 MB/s | 0.393 | 0.80 | 0.24–0.40 | -29% | fail |
| key | net.egress_stream | 3,802 | 1,007 MB/s | 0.274 | 0.80 | 0.25–0.28 | +51% | fail |
| key | net.bridge_stream | 3,707 | 1,514 MB/s | 0.437 | 0.80 | 0.37–0.44 | +30% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 2.99 | 12.1 seconds | 0.247 | 0.50 | 0.23–0.27 | -31% | fail |
| important | app.untar | 1.85 | 5.98 seconds | 0.309 | 0.50 | 0.26–0.45 | -90% | fail ~noisy |
| important | app.gzip | 1.93 | 3.04 seconds | 0.628 | 0.50 | 0.60–0.65 | +8% | pass |
| important | fs.metadata | 73,492 | 13,508 ops/s | 0.176 | 0.50 | 0.17–0.19 | -74% | fail |
| important | disk.bind_write | 164.3 | 135.0 MB/s | 0.822 | 0.50 | 0.78–0.86 | -17% | pass |
| important | disk.bind_read | 3,712 | 1,109 MB/s | 0.299 | 0.50 | 0.28–0.39 | -39% | fail |
| important | fs.bind_metadata | 79,181 | 2,438 ops/s | 0.0311 | 0.50 | 0.03–0.03 | - | fail |
| important | net.publish_rr | 19,404 | 6,480 round trips/s | 0.339 | 0.50 | 0.27–0.38 | -10% | fail |
| important | net.bridge_rr | 47,305 | 11,142 round trips/s | 0.236 | 0.50 | 0.21–0.31 | +33% | fail ~noisy |
| important | docker.run | ref 0.4 | 1.27 seconds | 0.315 | 0.50 | - | +337% | fail |
| important | docker.exec | ref 0.08 | 0.249 seconds | 0.321 | 0.50 | - | +29% | fail |
| important | docker.build_cached | ref 0.6 | 0.837 seconds | 0.717 | 0.50 | - | +462% | pass |
| important | docker.lifecycle | ref 1.2 | 1.4 seconds | 0.857 | 0.50 | - | +155% | pass |
| other | micro.syscall | 2,316,778 | 271,011 ops/s | 0.117 | 0.20 | 0.11–0.13 | +3% | fail |
| other | micro.forkexec | 4,670 | 526.6 ops/s | 0.11 | 0.20 | 0.11–0.12 | +2% | fail |
| other | micro.mmap | 191,339 | 10,086 ops/s | 0.0527 | 0.20 | 0.05–0.06 | +21% | fail |
| other | micro.clock | 41,925,566 | 38,339,598 ops/s | 0.91 | 0.20 | 0.85–0.96 | +8% | pass |
| other | micro.thread | 33,497 | 6,826 ops/s | 0.209 | 0.20 | 0.18–0.22 | -17% | pass |
| other | micro.pipe | 95,788 | 18,943 round trips/s | 0.2 | 0.20 | 0.16–0.24 | +90% | fail ~noisy |
| other | micro.signal | 437,649 | 105,249 ops/s | 0.244 | 0.20 | 0.23–0.25 | +69% | pass |
