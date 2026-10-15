# Scorecard `20261015-143352-754812b6-ci-vdso-virtiofs`

revision `754812b6413c` Merge codex/share; promote virtio-fs large requests into the series (patch 0032)

**Functional**: 132/135 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 4/10, important 4/13, other 3/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,052,233,740 | 3,876,289,370 ops/s | 0.942 | 0.80 | 0.89–1.02 | - | pass |
| key | cpu.multi | 14,706,613,400 | 15,496,601,700 ops/s | 1.09 | 0.80 | 0.94–1.12 | - | pass |
| key | mem.bandwidth | 5.45 | 5.39 GB/s | 0.989 | 0.80 | 0.94–1.02 | - | pass |
| key | mem.touch | 1.63 | 0.321 GB/s | 0.2 | 0.80 | 0.11–0.21 | - | fail |
| key | disk.seq_write | 102.3 | 78.4 MB/s | 0.766 | 0.80 | 0.63–0.99 | - | fail |
| key | disk.seq_read | 2,648 | 1,518 MB/s | 0.66 | 0.80 | 0.44–0.84 | - | fail |
| key | disk.volume_write | 117.9 | 119.8 MB/s | 0.996 | 0.80 | 0.68–1.08 | - | pass |
| key | net.publish_stream | 667.0 | 138.7 MB/s | 0.208 | 0.80 | 0.11–0.24 | +75% | fail |
| key | net.egress_stream | 1,633 | 34.3 MB/s | 0.0208 | 0.80 | 0.02–0.02 | -93% | fail |
| key | net.bridge_stream | 1,722 | 641.0 MB/s | 0.322 | 0.80 | 0.31–0.48 | -7% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.73 | 24.1 seconds | 0.161 | 0.50 | 0.14–0.17 | - | fail |
| important | app.untar | 1.97 | 4.09 seconds | 0.491 | 0.50 | 0.47–0.73 | - | fail |
| important | app.gzip | 2.12 | 3.61 seconds | 0.586 | 0.50 | 0.46–0.64 | - | pass |
| important | fs.metadata | 47,872 | 14,249 ops/s | 0.273 | 0.50 | 0.16–0.36 | - | fail |
| important | disk.bind_write | 162.0 | 147.8 MB/s | 0.975 | 0.50 | 0.82–1.02 | - | pass |
| important | disk.bind_read | 3,854 | 1,617 MB/s | 0.396 | 0.50 | 0.32–0.60 | - | fail |
| important | fs.bind_metadata | 41,844 | 573.4 ops/s | 0.0171 | 0.50 | 0.01–0.04 | - | fail |
| important | net.publish_rr | 20,470 | 7,292 round trips/s | 0.36 | 0.50 | 0.33–0.39 | +129% | fail |
| important | net.bridge_rr | 52,745 | 9,475 round trips/s | 0.18 | 0.50 | 0.14–0.20 | +8% | fail |
| important | docker.run | ref 0.4 | 1.5 seconds | 0.267 | 0.50 | - | - | fail |
| important | docker.exec | ref 0.08 | 0.288 seconds | 0.277 | 0.50 | - | - | fail |
| important | docker.build_cached | ref 0.6 | 1.03 seconds | 0.585 | 0.50 | - | - | pass |
| important | docker.lifecycle | ref 1.2 | 1.88 seconds | 0.64 | 0.50 | - | - | pass |
| other | micro.syscall | 1,836,424 | 242,433 ops/s | 0.133 | 0.20 | 0.13–0.14 | - | fail |
| other | micro.forkexec | 3,513 | 509.0 ops/s | 0.141 | 0.20 | 0.10–0.17 | - | fail |
| other | micro.mmap | 187,820 | 21,779 ops/s | 0.116 | 0.20 | 0.11–0.13 | - | fail |
| other | micro.clock | 37,879,538 | 30,599,681 ops/s | 0.771 | 0.20 | 0.74–0.88 | - | pass |
| other | micro.thread | 22,176 | 5,649 ops/s | 0.266 | 0.20 | 0.23–0.27 | - | pass |
| other | micro.pipe | 119,594 | 12,422 round trips/s | 0.123 | 0.20 | 0.07–0.15 | - | fail |
| other | micro.signal | 394,694 | 91,294 ops/s | 0.231 | 0.20 | 0.22–0.25 | - | pass |
