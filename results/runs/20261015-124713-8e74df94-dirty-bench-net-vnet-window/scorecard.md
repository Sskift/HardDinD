# Scorecard `20261015-124713-8e74df94-dirty-bench-net-vnet-window`

revision `8e74df94d944` (dirty) Stop tracking run summaries (main tracks complete ci runs only)
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
| key | net.publish_stream | 2,678 | 340.7 MB/s | 0.127 | 0.80 | 0.09–0.16 | +133% | fail |
| key | net.egress_stream | 3,263 | 737.6 MB/s | 0.226 | 0.80 | 0.23–0.23 | +783% | fail |
| key | net.bridge_stream | 2,896 | 1,158 MB/s | 0.4 | 0.80 | 0.35–0.45 | -29% | fail |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | - | -  | - | 0.50 | - | - | not_tested |
| important | app.untar | - | -  | - | 0.50 | - | - | not_tested |
| important | app.gzip | - | -  | - | 0.50 | - | - | not_tested |
| important | fs.metadata | - | -  | - | 0.50 | - | - | not_tested |
| important | net.publish_rr | 53,597 | 4,909 round trips/s | 0.0916 | 0.50 | 0.04–0.15 | -38% | fail |
| important | net.bridge_rr | 51,870 | 8,231 round trips/s | 0.159 | 0.50 | 0.11–0.20 | +83% | fail |
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
