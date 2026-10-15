# Scorecard `20261015-145300-a63ab345-ci-faultaround`

revision `a63ab34551f5` Pre-commit hook runs ./hd check
candidates: anon-faultaround, core-faultaround-edges; kernel args: uml_faultaround=5 uml_faultaround_edges=1

**Functional**: 132/135 pass, 3 fail, 0 not tested → FAIL

**Performance** (guest/native, higher is better): key 5/10, important 3/13, other 2/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 3,102,181,660 | 2,985,374,360 ops/s | 0.986 | 0.80 | 0.81–1.44 | +5% | pass |
| key | cpu.multi | 9,593,204,640 | 9,192,239,480 ops/s | 0.958 | 0.80 | 0.80–1.11 | -12% | pass |
| key | mem.bandwidth | 4.82 | 3.55 GB/s | 0.877 | 0.80 | 0.56–1.46 | -11% | pass |
| key | mem.touch | 1.63 | 0.541 GB/s | 0.342 | 0.80 | 0.17–0.45 | +71% | fail |
| key | disk.seq_write | 146.5 | 117.0 MB/s | 0.923 | 0.80 | 0.62–1.03 | +20% | pass |
| key | disk.seq_read | 2,610 | 1,924 MB/s | 0.586 | 0.80 | 0.50–0.91 | -11% | fail |
| key | disk.volume_write | 134.6 | 120.8 MB/s | 0.866 | 0.80 | 0.74–1.01 | -13% | pass |
| key | net.publish_stream | 1,177 | 176.2 MB/s | 0.161 | 0.80 | 0.15–0.21 | -23% | fail |
| key | net.egress_stream | 2,809 | 58.2 MB/s | 0.0215 | 0.80 | 0.02–0.02 | +3% | fail |
| key | net.bridge_stream | 3,072 | 1,137 MB/s | 0.369 | 0.80 | 0.34–0.45 | +15% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 3.92 | 13.0 seconds | 0.318 | 0.50 | 0.27–0.33 | +98% | fail |
| important | app.untar | 3.82 | 7.45 seconds | 0.512 | 0.50 | 0.37–1.23 | +4% | pass |
| important | app.gzip | 2.11 | 4.08 seconds | 0.566 | 0.50 | 0.45–0.62 | -3% | pass |
| important | fs.metadata | 67,391 | 12,647 ops/s | 0.179 | 0.50 | 0.16–0.23 | -34% | fail |
| important | disk.bind_write | 129.9 | 127.7 MB/s | 0.938 | 0.50 | 0.65–1.09 | -4% | pass |
| important | disk.bind_read | 3,061 | 972.0 MB/s | 0.405 | 0.50 | 0.11–0.75 | +2% | fail |
| important | fs.bind_metadata | - | -  | - | 0.50 | - | - | fail (RuntimeError: docker run --rm --name: ti) |
| important | net.publish_rr | 18,174 | 2,847 round trips/s | 0.142 | 0.50 | 0.09–0.19 | -61% | fail |
| important | net.bridge_rr | 42,241 | 3,130 round trips/s | 0.0786 | 0.50 | 0.01–0.17 | -56% | fail |
| important | docker.run | ref 0.4 | 3.2 seconds | 0.125 | 0.50 | - | -53% | fail |
| important | docker.exec | ref 0.08 | 0.7 seconds | 0.114 | 0.50 | - | -59% | fail |
| important | docker.build_cached | ref 0.6 | 4.28 seconds | 0.14 | 0.50 | - | -76% | fail |
| important | docker.lifecycle | ref 1.2 | 6.2 seconds | 0.194 | 0.50 | - | -70% | fail |
| other | micro.syscall | 1,800,082 | 220,147 ops/s | 0.125 | 0.20 | 0.09–0.14 | -6% | fail |
| other | micro.forkexec | 3,199 | 208.0 ops/s | 0.0775 | 0.20 | 0.07–0.11 | -45% | fail |
| other | micro.mmap | 121,562 | 4,392 ops/s | 0.0367 | 0.20 | 0.03–0.05 | -68% | fail |
| other | micro.clock | 19,453,458 | 21,066,308 ops/s | 1.11 | 0.20 | 0.86–1.34 | +44% | pass |
| other | micro.thread | 14,048 | 3,045 ops/s | 0.208 | 0.20 | 0.17–0.24 | -22% | pass |
| other | micro.pipe | 115,102 | 11,596 round trips/s | 0.103 | 0.20 | 0.09–0.12 | -17% | fail |
| other | micro.signal | 324,640 | 49,119 ops/s | 0.151 | 0.20 | 0.14–0.20 | -35% | fail |
