# Scorecard `20261015-114450-1abb922b-dirty-bench-net-vnet-tx`

revision `1abb922b5032` (dirty) Merge commit '33daff2' into codex/net
candidates: net-fd-vnet; kernel args: -

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
| key | net.publish_stream | 3,096 | 352.2 MB/s | 0.114 | 0.80 | 0.11–0.12 | +37% | fail |
| key | net.egress_stream | 3,141 | 253.6 MB/s | 0.0807 | 0.80 | 0.06–0.10 | +298% | fail |
| key | net.bridge_stream | 3,196 | 1,227 MB/s | 0.384 | 0.80 | 0.36–0.41 | -29% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | - | -  | - | 0.50 | - | - | not_tested |
| important | app.untar | - | -  | - | 0.50 | - | - | not_tested |
| important | app.gzip | - | -  | - | 0.50 | - | - | not_tested |
| important | fs.metadata | - | -  | - | 0.50 | - | - | not_tested |
| important | net.publish_rr | 56,578 | 3,752 round trips/s | 0.0663 | 0.50 | 0.01–0.11 | -58% | fail |
| important | net.bridge_rr | 48,750 | 9,071 round trips/s | 0.186 | 0.50 | 0.16–0.23 | -6% | fail |
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
