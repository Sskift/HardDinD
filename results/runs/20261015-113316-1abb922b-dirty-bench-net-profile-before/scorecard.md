# Scorecard `20261015-113316-1abb922b-dirty-bench-net-profile-before`

revision `1abb922b5032` (dirty) Merge commit '33daff2' into codex/net

**Performance** (guest/native, higher is better): key 0/10, important 0/10, other 0/7

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | - | -  | - | 0.80 | - | - | not_tested |
| key | cpu.multi | - | -  | - | 0.80 | - | - | not_tested |
| key | mem.bandwidth | - | -  | - | 0.80 | - | - | not_tested |
| key | mem.touch | - | -  | - | 0.80 | - | - | not_tested |
| key | disk.seq_write | - | -  | - | 0.80 | - | - | not_tested |
| key | disk.seq_read | - | -  | - | 0.80 | - | - | not_tested |
| key | disk.volume_write | - | -  | - | 0.80 | - | - | not_tested |
| key | net.publish_stream | 2,926 | 237.9 MB/s | 0.0813 | 0.80 | 0.08–0.08 | +78% | fail |
| key | net.egress_stream | 2,700 | 106.6 MB/s | 0.0395 | 0.80 | 0.04–0.04 | +69% | fail |
| key | net.bridge_stream | 3,102 | 1,668 MB/s | 0.538 | 0.80 | 0.47–0.62 | +2% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | - | -  | - | 0.50 | - | - | not_tested |
| important | app.untar | - | -  | - | 0.50 | - | - | not_tested |
| important | app.gzip | - | -  | - | 0.50 | - | - | not_tested |
| important | fs.metadata | - | -  | - | 0.50 | - | - | not_tested |
| important | net.publish_rr | 50,775 | 8,997 round trips/s | 0.177 | 0.50 | 0.16–0.19 | +20% | fail |
| important | net.bridge_rr | 53,466 | 5,413 round trips/s | 0.101 | 0.50 | 0.06–0.14 | -43% | fail |
| important | docker.run | - | -  | - | 0.50 | - | - | not_tested |
| important | docker.exec | - | -  | - | 0.50 | - | - | not_tested |
| important | docker.build_cached | - | -  | - | 0.50 | - | - | not_tested |
| important | docker.lifecycle | - | -  | - | 0.50 | - | - | not_tested |
| other | micro.syscall | - | -  | - | 0.20 | - | - | not_tested |
| other | micro.forkexec | - | -  | - | 0.20 | - | - | not_tested |
| other | micro.mmap | - | -  | - | 0.20 | - | - | not_tested |
| other | micro.clock | - | -  | - | 0.20 | - | - | not_tested |
| other | micro.thread | - | -  | - | 0.20 | - | - | not_tested |
| other | micro.pipe | - | -  | - | 0.20 | - | - | not_tested |
| other | micro.signal | - | -  | - | 0.20 | - | - | not_tested |
