# Scorecard `20261016-030029-a173e9b9-ci-default-net-promoted`

revision `a173e9b909c2` Promote net-vector-poll and net-idle-wait into the series and default boot args (patches 0038, 0039)

**Functional**: 151/154 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 6/10, important 5/13, other 2/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,137,972,790 | 4,057,243,780 ops/s | 0.979 | 0.80 | 0.96–1.00 | -1% | pass |
| key | cpu.multi | 16,154,769,000 | 15,925,376,100 ops/s | 0.995 | 0.80 | 0.97–1.00 | +4% | pass |
| key | mem.bandwidth | 5.23 | 5.22 GB/s | 0.994 | 0.80 | 0.98–1.17 | +3% | pass |
| key | mem.touch | 1.47 | 0.668 GB/s | 0.424 | 0.80 | 0.26–0.49 | +4% | fail ~noisy |
| key | disk.seq_write | 156.7 | 134.9 MB/s | 0.825 | 0.80 | 0.81–1.04 | -1% | pass |
| key | disk.seq_read | 3,704 | 2,899 MB/s | 0.788 | 0.80 | 0.70–0.82 | +1% | fail |
| key | disk.volume_write | 145.3 | 153.5 MB/s | 0.983 | 0.80 | 0.97–1.15 | +5% | pass |
| key | net.publish_stream | 1,109 | 1,108 MB/s | 0.999 | 0.80 | 0.77–1.33 | -3% | pass ~noisy |
| key | net.egress_stream | 3,444 | 1,064 MB/s | 0.313 | 0.80 | 0.26–0.34 | +13% | fail ~noisy |
| key | net.bridge_stream | 3,338 | 1,768 MB/s | 0.53 | 0.80 | 0.47–0.55 | +3% | fail ~noisy |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 4.22 | 18.1 seconds | 0.228 | 0.50 | 0.20–0.28 | +26% | fail ~noisy |
| important | app.untar | 2.16 | 3.69 seconds | 0.601 | 0.50 | 0.54–0.88 | -2% | pass ~noisy |
| important | app.gzip | 2.06 | 3.21 seconds | 0.634 | 0.50 | 0.63–0.71 | +4% | pass |
| important | fs.metadata | 59,360 | 19,104 ops/s | 0.322 | 0.50 | 0.30–0.37 | -8% | fail |
| important | disk.bind_write | 158.6 | 142.2 MB/s | 0.9 | 0.50 | 0.87–1.00 | -4% | pass |
| important | disk.bind_read | 3,551 | 1,663 MB/s | 0.465 | 0.50 | 0.42–0.51 | -4% | fail |
| important | fs.bind_metadata | 60,670 | 1,800 ops/s | 0.0294 | 0.50 | 0.03–0.03 | -7% | fail |
| important | net.publish_rr | 24,103 | 10,160 round trips/s | 0.426 | 0.50 | 0.39–0.49 | +12% | fail ~noisy |
| important | net.bridge_rr | 55,627 | 9,467 round trips/s | 0.215 | 0.50 | 0.13–0.29 | +19% | fail ~noisy |
| important | docker.run | ref 0.4 | 1.13 seconds | 0.353 | 0.50 | - | +20% | fail ~noisy |
| important | docker.exec | ref 0.08 | 0.297 seconds | 0.27 | 0.50 | - | +8% | fail |
| important | docker.build_cached | ref 0.6 | 0.803 seconds | 0.748 | 0.50 | - | +2% | pass |
| important | docker.lifecycle | ref 1.2 | 1.58 seconds | 0.762 | 0.50 | - | -12% | pass |
| other | micro.syscall | 1,917,873 | 248,066 ops/s | 0.126 | 0.20 | 0.10–0.14 | +3% | fail ~noisy |
| other | micro.forkexec | 3,946 | 591.4 ops/s | 0.144 | 0.20 | 0.12–0.15 | -1% | fail ~noisy |
| other | micro.mmap | 190,152 | 18,163 ops/s | 0.091 | 0.20 | 0.07–0.15 | -19% | fail ~noisy |
| other | micro.clock | 39,605,331 | 28,306,779 ops/s | 0.72 | 0.20 | 0.69–0.77 | +2% | pass ~noisy |
| other | micro.thread | 27,950 | 7,990 ops/s | 0.28 | 0.20 | 0.20–0.38 | -5% | pass ~noisy |
| other | micro.pipe | 95,065 | 19,626 round trips/s | 0.198 | 0.20 | 0.14–0.25 | +32% | fail ~noisy |
| other | micro.signal | 409,882 | 81,324 ops/s | 0.194 | 0.20 | 0.13–0.26 | -14% | fail ~noisy |
