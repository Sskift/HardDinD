# Scorecard `20261015-234159-ef2f21b1-ci-norseq-faultaround`

revision `ef2f21b19984` Turn rseq back off: fork+exec in guests regressed from ~2 ms to ~85 ms
candidates: anon-faultaround, core-faultaround-edges; kernel args: uml_faultaround=5 uml_faultaround_edges=1

**Functional**: 152/154 pass, 2 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 7/10, important 4/13, other 3/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,125,330,290 | 3,806,164,600 ops/s | 0.934 | 0.80 | 0.84–1.06 | -4% | pass |
| key | cpu.multi | 14,962,697,600 | 15,653,547,200 ops/s | 0.973 | 0.80 | 0.96–1.08 | +14% | pass |
| key | mem.bandwidth | 3.63 | 3.73 GB/s | 1.03 | 0.80 | 0.91–1.19 | +3% | pass ~noisy |
| key | mem.touch | 1.35 | 0.47 GB/s | 0.321 | 0.80 | 0.28–0.52 | +61% | fail ~noisy |
| key | disk.seq_write | 142.6 | 138.9 MB/s | 0.893 | 0.80 | 0.83–1.16 | +2% | pass ~noisy |
| key | disk.seq_read | 2,682 | 2,419 MB/s | 0.939 | 0.80 | 0.58–1.19 | +48% | pass ~noisy |
| key | disk.volume_write | 152.8 | 150.4 MB/s | 0.975 | 0.80 | 0.94–1.03 | -4% | pass |
| key | net.publish_stream | 1,172 | 1,148 MB/s | 1.01 | 0.80 | 0.58–1.03 | +39% | pass ~noisy |
| key | net.egress_stream | 3,466 | 757.9 MB/s | 0.221 | 0.80 | 0.15–0.25 | -15% | fail |
| key | net.bridge_stream | 3,584 | 1,148 MB/s | 0.32 | 0.80 | 0.29–0.42 | -27% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.5 | 13.5 seconds | 0.25 | 0.50 | 0.23–0.29 | +63% | fail |
| important | app.untar | 2.03 | 5.46 seconds | 0.378 | 0.50 | 0.31–0.47 | -31% | fail ~noisy |
| important | app.gzip | 2.02 | 3.28 seconds | 0.621 | 0.50 | 0.60–0.64 | +3% | pass |
| important | fs.metadata | 75,172 | 15,419 ops/s | 0.208 | 0.50 | 0.19–0.23 | -45% | fail |
| important | disk.bind_write | 139.4 | 130.0 MB/s | 0.93 | 0.50 | 0.90–0.93 | +0% | pass |
| important | disk.bind_read | 4,033 | 1,538 MB/s | 0.383 | 0.50 | 0.38–0.40 | -14% | fail |
| important | fs.bind_metadata | 72,915 | 723.0 ops/s | 0.01 | 0.50 | 0.01–0.01 | -28% | fail |
| important | net.publish_rr | 22,358 | 4,822 round trips/s | 0.259 | 0.50 | 0.16–0.32 | -25% | fail ~noisy |
| important | net.bridge_rr | 42,983 | 7,872 round trips/s | 0.168 | 0.50 | 0.14–0.23 | +13% | fail ~noisy |
| important | docker.run | ref 0.4 | 1.44 seconds | 0.277 | 0.50 | - | +23% | fail |
| important | docker.exec | ref 0.08 | 0.285 seconds | 0.281 | 0.50 | - | +26% | fail |
| important | docker.build_cached | ref 0.6 | 0.753 seconds | 0.797 | 0.50 | - | +28% | pass |
| important | docker.lifecycle | ref 1.2 | 1.77 seconds | 0.68 | 0.50 | - | +17% | pass |
| other | micro.syscall | 2,144,836 | 259,296 ops/s | 0.121 | 0.20 | 0.09–0.12 | -4% | fail |
| other | micro.forkexec | 4,485 | 420.9 ops/s | 0.097 | 0.20 | 0.08–0.10 | +3364% | fail |
| other | micro.mmap | 184,393 | 9,940 ops/s | 0.055 | 0.20 | 0.05–0.06 | -60% | fail |
| other | micro.clock | 38,529,190 | 32,816,356 ops/s | 0.852 | 0.20 | 0.81–0.95 | +20% | pass |
| other | micro.thread | 26,424 | 5,976 ops/s | 0.218 | 0.20 | 0.20–0.23 | -10% | pass |
| other | micro.pipe | 124,534 | 13,962 round trips/s | 0.112 | 0.20 | 0.11–0.13 | -20% | fail |
| other | micro.signal | 420,255 | 98,282 ops/s | 0.234 | 0.20 | 0.22–0.25 | +10% | pass |
