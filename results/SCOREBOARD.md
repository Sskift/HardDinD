# Scorecard `20261026-094922-303aaee7-ci-candidate-sorted-populate`

revision `303aaee79bc0` perf(um): add bounded contiguous chunks for sorted fault pools
candidates: fault-sorted-alloc, fault-populate; kernel args: uml_fault_sorted=1 uml_fault_populate=1

**Functional**: 172/175 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 5/13, other 2/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,115,781,920 | 3,895,377,950 ops/s | 0.946 | 0.80 | 0.89–0.99 | -4% | pass |
| key | cpu.multi | 14,877,762,400 | 14,422,773,200 ops/s | 0.972 | 0.80 | 0.96–0.98 | -2% | pass |
| key | mem.bandwidth | 5.33 | 5.02 GB/s | 0.959 | 0.80 | 0.94–1.02 | +1% | pass |
| key | mem.touch | 1.68 | 0.682 GB/s | 0.415 | 0.80 | 0.40–0.47 | +104% | fail |
| key | disk.seq_write | 156.9 | 135.4 MB/s | 0.852 | 0.80 | 0.74–0.89 | -1% | pass |
| key | disk.seq_read | 4,075 | 2,776 MB/s | 0.7 | 0.80 | 0.66–0.76 | +35% | fail |
| key | disk.volume_write | 138.7 | 144.7 MB/s | 1.08 | 0.80 | 0.84–1.23 | +12% | pass ~noisy |
| key | net.publish_stream | 2,896 | 1,043 MB/s | 0.355 | 0.80 | 0.33–0.38 | -10% | fail |
| key | net.egress_stream | 3,614 | 812.5 MB/s | 0.221 | 0.80 | 0.18–0.23 | -19% | fail |
| key | net.bridge_stream | 3,730 | 1,648 MB/s | 0.45 | 0.80 | 0.40–0.46 | +3% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.15 | 12.7 seconds | 0.255 | 0.50 | 0.24–0.27 | +4% | fail |
| important | app.untar | 3.71 | 6 seconds | 0.654 | 0.50 | 0.40–0.67 | +112% | pass ~noisy |
| important | app.gzip | 2.02 | 3.19 seconds | 0.62 | 0.50 | 0.62–0.67 | -1% | pass |
| important | fs.metadata | 72,839 | 14,148 ops/s | 0.192 | 0.50 | 0.19–0.21 | +9% | fail |
| important | disk.bind_write | 170.5 | 139.7 MB/s | 0.836 | 0.50 | 0.80–0.86 | +2% | pass |
| important | disk.bind_read | 3,517 | 1,409 MB/s | 0.405 | 0.50 | 0.31–0.49 | +36% | fail |
| important | fs.bind_metadata | 76,045 | 2,229 ops/s | 0.0292 | 0.50 | 0.03–0.03 | -6% | fail |
| important | net.publish_rr | 20,458 | 6,813 round trips/s | 0.333 | 0.50 | 0.31–0.36 | -2% | fail |
| important | net.bridge_rr | 47,945 | 10,579 round trips/s | 0.215 | 0.50 | 0.21–0.26 | -9% | fail |
| important | docker.run | ref 0.4 | 1.15 seconds | 0.347 | 0.50 | - | +10% | fail |
| important | docker.exec | ref 0.08 | 0.239 seconds | 0.335 | 0.50 | - | +4% | fail |
| important | docker.build_cached | ref 0.6 | 0.84 seconds | 0.714 | 0.50 | - | -0% | pass |
| important | docker.lifecycle | ref 1.2 | 1.32 seconds | 0.909 | 0.50 | - | +6% | pass |
| other | micro.syscall | 2,230,831 | 264,547 ops/s | 0.119 | 0.20 | 0.10–0.12 | +1% | fail |
| other | micro.forkexec | 4,571 | 470.4 ops/s | 0.105 | 0.20 | 0.10–0.11 | -5% | fail |
| other | micro.mmap | 186,026 | 10,491 ops/s | 0.0577 | 0.20 | 0.05–0.06 | +9% | fail |
| other | micro.clock | 41,277,910 | 35,339,494 ops/s | 0.872 | 0.20 | 0.74–0.91 | -4% | pass |
| other | micro.thread | 25,242 | 5,288 ops/s | 0.203 | 0.20 | 0.17–0.23 | -3% | pass ~noisy |
| other | micro.pipe | 129,181 | 15,084 round trips/s | 0.118 | 0.20 | 0.11–0.14 | -41% | fail |
| other | micro.signal | 418,105 | 79,065 ops/s | 0.19 | 0.20 | 0.16–0.24 | -22% | fail |
