# Scorecard `20261015-172709-fc0d109c-ci-vnet-default`

revision `fc0d109c987a` Promote the vnet network link into the default build (patch 0034)

**Functional**: 132/136 pass, 4 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 3/13, other 2/7

`~noisy`: native samples varied by more than 30% during the run (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 3,786,344,160 | 3,456,368,680 ops/s | 0.991 | 0.80 | 0.87–1.04 | +0% | pass |
| key | cpu.multi | 14,343,304,400 | 13,678,099,800 ops/s | 0.978 | 0.80 | 0.91–1.13 | +2% | pass ~noisy |
| key | mem.bandwidth | 5.37 | 5.11 GB/s | 0.976 | 0.80 | 0.45–1.33 | +11% | pass ~noisy |
| key | mem.touch | 1.41 | 0.103 GB/s | 0.085 | 0.80 | 0.06–0.11 | -75% | fail ~noisy |
| key | disk.seq_write | 113.9 | 123.5 MB/s | 1.09 | 0.80 | 0.87–1.33 | +18% | pass |
| key | disk.seq_read | 3,684 | 3,062 MB/s | 0.775 | 0.80 | 0.55–1.42 | +32% | fail ~noisy |
| key | disk.volume_write | 144.7 | 119.5 MB/s | 0.955 | 0.80 | 0.27–1.37 | +10% | pass ~noisy |
| key | net.publish_stream | 1,059 | 822.4 MB/s | 0.754 | 0.80 | 0.52–0.88 | +368% | fail |
| key | net.egress_stream | 3,036 | 619.2 MB/s | 0.213 | 0.80 | 0.17–0.23 | +893% | fail |
| key | net.bridge_stream | 2,606 | 1,189 MB/s | 0.4 | 0.80 | 0.27–0.54 | +8% | fail ~noisy |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 4 | 18.0 seconds | 0.218 | 0.50 | 0.20–0.26 | -31% | fail |
| important | app.untar | 8.28 | 5.58 seconds | 1.48 | 0.50 | 0.48–1.77 | +190% | pass ~noisy |
| important | app.gzip | 2.02 | 3.2 seconds | 0.62 | 0.50 | 0.62–0.64 | +9% | pass |
| important | fs.metadata | 55,529 | 13,411 ops/s | 0.253 | 0.50 | 0.18–0.39 | +41% | fail ~noisy |
| important | disk.bind_write | 126.5 | 94.0 MB/s | 0.744 | 0.50 | 0.48–0.90 | -21% | pass ~noisy |
| important | disk.bind_read | 3,119 | 1,303 MB/s | 0.418 | 0.50 | 0.21–0.57 | +3% | fail ~noisy |
| important | fs.bind_metadata | 62,319 | 702.2 ops/s | 0.016 | 0.50 | 0.01–0.02 | - | fail ~noisy |
| important | net.publish_rr | 4,285 | 437.4 round trips/s | 0.125 | 0.50 | 0.01–0.45 | -12% | fail ~noisy |
| important | net.bridge_rr | 34,866 | 5,022 round trips/s | 0.148 | 0.50 | 0.08–0.19 | +88% | fail ~noisy |
| important | docker.run | ref 0.4 | 3.42 seconds | 0.117 | 0.50 | - | -6% | fail |
| important | docker.exec | ref 0.08 | 0.455 seconds | 0.176 | 0.50 | - | +54% | fail |
| important | docker.build_cached | ref 0.6 | 7.3 seconds | 0.0822 | 0.50 | - | -41% | fail |
| important | docker.lifecycle | ref 1.2 | 6.44 seconds | 0.186 | 0.50 | - | -4% | fail |
| other | micro.syscall | 1,543,068 | 183,397 ops/s | 0.12 | 0.20 | 0.10–0.14 | -4% | fail ~noisy |
| other | micro.forkexec | 2,533 | 277.2 ops/s | 0.107 | 0.20 | 0.08–0.13 | +39% | fail ~noisy |
| other | micro.mmap | 139,826 | 7,411 ops/s | 0.053 | 0.20 | 0.05–0.06 | +44% | fail ~noisy |
| other | micro.clock | 23,273,859 | 24,192,055 ops/s | 1.04 | 0.20 | 0.87–1.20 | -6% | pass ~noisy |
| other | micro.thread | 20,256 | 4,516 ops/s | 0.217 | 0.20 | 0.21–0.23 | +4% | pass |
| other | micro.pipe | 107,184 | 12,672 round trips/s | 0.117 | 0.20 | 0.08–0.12 | +14% | fail |
| other | micro.signal | 316,872 | 55,090 ops/s | 0.177 | 0.20 | 0.15–0.24 | +17% | fail ~noisy |
