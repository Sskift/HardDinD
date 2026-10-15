# Scorecard `20261015-141118-f8cbf700-dirty-ci-net-final-profiled`

revision `f8cbf7007e3f` (dirty) Add opt-in fd vnet offloads and scaled slirp TCP windows
candidates: net-fd-vnet; kernel args: -

**Functional**: 14/130 pass, 0 fail, 116 not tested → FAIL

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
| key | net.publish_stream | 3,289 | 402.8 MB/s | 0.122 | 0.80 | 0.02–0.22 | +62% | fail |
| key | net.egress_stream | 2,981 | 828.0 MB/s | 0.278 | 0.80 | 0.26–0.29 | +1011% | fail |
| key | net.bridge_stream | 3,238 | 1,134 MB/s | 0.35 | 0.80 | 0.32–0.37 | -30% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | - | -  | - | 0.50 | - | - | not_tested |
| important | app.untar | - | -  | - | 0.50 | - | - | not_tested |
| important | app.gzip | - | -  | - | 0.50 | - | - | not_tested |
| important | fs.metadata | - | -  | - | 0.50 | - | - | not_tested |
| important | net.publish_rr | 58,498 | 9,192 round trips/s | 0.157 | 0.50 | 0.16–0.16 | +55% | fail |
| important | net.bridge_rr | 58,022 | 9,553 round trips/s | 0.165 | 0.50 | 0.12–0.21 | -2% | fail |
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
