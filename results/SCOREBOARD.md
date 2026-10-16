# Scorecard `20261016-144711-745232c0-ci-fault-share-promoted`

revision `745232c09031` kernel: promote fault mapping and share latency defaults

**Host was CPU-throttled** (functional quota periods throttled: 66.6%; pre-boot: {'quiet': True, 'waited': 741, 'throttled': [0.815, 0.96, 0.935, 0.9, 1.0, 1.0, 0.855, 0.905, 0.99, 1.0, 1.0, 0.355, 0.01]}); functional result is not decision-grade.

**Functional**: 172/175 pass, 3 fail, 0 not tested → FAIL

Checks throttled in more than 10% of quota periods:

- `network_backend_selftest`: 41.4%
- `container.lifecycle`: 83.3%
- `container.exit_stdin_wait`: 98.9%
- `container.exec_attach`: 100.0%
- `container.tty`: 100.0%
- `container.rename`: 100.0%
- `container.pause`: 100.0%
- `container.top_stats`: 100.0%
- `container.logs_follow`: 100.0%
- `container.signals`: 100.0%
- `container.cp`: 100.0%
- `image.commit_export`: 100.0%
- `image.save_load_tag`: 100.0%
- `image.buildkit`: 97.0%
- `image.pull`: 100.0%
- `image.registry_roundtrip`: 82.8%
- `volume.shared`: 74.1%
- `storage.bind_metadata`: 89.2%
- `security.readonly_user`: 100.0%
- `security.seccomp`: 83.2%
- `security.privileged_mount`: 98.3%
- `security.net_admin`: 100.0%
- `security.user_namespace`: 90.7%
- `resource.memory_oom`: 96.0%
- `resource.pids`: 84.4%
- `resource.cpu_quota`: 98.1%
- `resource.cpuset`: 95.1%
- `resource.update`: 53.5%
- `network.bridge_dns`: 98.9%
- `network.ipv6`: 100.0%
- `network.publish_tcp_udp`: 99.2%
- `network.publish_conflicts`: 58.3%
- `network.publish_addresses`: 12.8%
- `network.publish_wildcard`: 46.4%
- `system.prune`: 82.5%
- `process.dispatch_identity`: 10.5%
- `process.dispatch_fdio`: 28.6%
- `process.core_processes`: 92.4%
- `process.core_signals`: 82.9%
- `process.core_exec`: 43.7%
- `process.core_syscall`: 42.3%
- `process.core_faults`: 97.0%
- `process.core_fpstate`: 96.3%
- `process.core_syscalls_filters`: 44.8%
- `process.core_patch`: 11.9%
- `process.core_dynamic`: 100.0%
- `process.core_hot`: 100.0%
- `process.core_startup_pressure`: 45.3%
- `process.core_startup_burst`: 76.4%
- `process.core_compat`: 50.9%
- `compose.workflow`: 62.8%
- `swarm.workflow`: 83.9%
- `network.publish_alias_lifetime`: 93.1%
- `network.manual_forward_ownership`: 93.9%
- `kernel.clock_ordinary`: 76.0%
- `kernel.clock_privileged`: 60.0%
- `kernel.rseq`: 97.0%
- `share.files.read_initial`: 100.0%
- `share.files.outer_write_open_fd`: 100.0%
- `share.files.outer_write_reopen`: 100.0%
- `share.files.outer_truncate_fstat`: 93.7%
- `share.files.outer_chmod_fstat`: 41.9%
- `share.files.guest_write_outer_read`: 100.0%
- `share.files.mmap_outer_to_guest`: 83.3%
- `share.files.mmap_guest_to_outer`: 60.6%
- `share.files.replace_open_inode`: 100.0%
- `share.files.unlink_open_inode`: 100.0%
- `share.files.xattr_both_directions`: 100.0%
- `share.files.outer_lock_blocks_guest`: 100.0%
- `share.files.guest_lock_blocks_outer`: 100.0%
- `share.files.outer_inotify_to_guest`: 100.0%
- `share.files.user_permission_denied`: 72.2%
- `share.files.user_create_owner`: 63.6%
- `share.files.read_only_bind`: 88.5%
- `share.files.outer_inotify_to_guest_uid1001`: 44.1%
- `share.files.guest_inotify_to_outer_uid1001`: 42.4%
- `share.files.parallel_creation_masks`: 70.9%
- `share.files.outer_metadata_churn`: 97.8%
- `share.files.outer_replace_churn`: 100.0%
- `share.files.outer_negative_lookup`: 100.0%
- `share.files.outer_mtime`: 96.2%
- `share.files.outer_permission_revoked`: 100.0%
- `share.files.outer_mmap_window`: 100.0%
- `share.files.outer_attr_window`: 100.0%
- `share.files.outer_read_window`: 100.0%
- `share.files.outer_mapped_write`: 100.0%
- `share.files.notification_mmap_latency`: 62.1%
- `share.files.outer_directory_inotify`: 26.5%
- `share.files.outer_hardlink_alias`: 100.0%
- `share.files.outer_rename_unwatched`: 100.0%
- `share.files.outer_before_first_lookup`: 80.0%
- `share.files.outer_watch_exhaustion`: 100.0%
- `share.files.outer_notify_overflow`: 61.9%
- `share.files.outer_mapped_remap`: 50.0%
- `share.files.outer_mapped_scale`: 97.2%
- `share.files.outer_mapped_read`: 76.9%
- `share.locks.kill_cancels_wait`: 13.3%
- `share.locks.rename_keeps_lock`: 11.1%

**Performance** (guest/native, higher is better): key 5/10, important 4/13, other 2/7

`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled in more than 10% of quota periods during a sample (host contention).

| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |
|---|---|---:|---:|---:|---:|---|---:|---|
| key | cpu.single | 4,073,317,070 | 3,845,048,910 ops/s | 0.941 | 0.80 | 0.84–1.52 | - | pass ~noisy |
| key | cpu.multi | 13,513,845,700 | 12,735,166,700 ops/s | 0.952 | 0.80 | 0.88–0.99 | - | pass ~noisy |
| key | mem.bandwidth | 5.24 | 4.96 GB/s | 0.939 | 0.80 | 0.68–1.61 | - | pass ~noisy |
| key | mem.touch | 1.68 | 1.25 GB/s | 0.693 | 0.80 | 0.54–0.96 | - | fail ~noisy |
| key | disk.seq_write | 140.0 | 123.4 MB/s | 0.846 | 0.80 | 0.70–1.01 | - | pass ~noisy |
| key | disk.seq_read | 4,062 | 3,052 MB/s | 0.753 | 0.80 | 0.67–1.40 | - | fail ~noisy |
| key | disk.volume_write | 139.6 | 124.7 MB/s | 0.806 | 0.80 | 0.29–0.99 | - | pass ~noisy |
| key | net.publish_stream | 707.0 | 338.3 MB/s | 0.552 | 0.80 | 0.30–0.56 | - | fail ~noisy |
| key | net.egress_stream | 2,570 | 575.4 MB/s | 0.181 | 0.80 | 0.02–0.29 | - | fail ~noisy |
| key | net.bridge_stream | 3,097 | 1,112 MB/s | 0.336 | 0.80 | 0.31–0.45 | - | fail ~noisy |
| key | gpu.compute | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.h2d | - | -  | - | 0.80 | - | - | not_tested |
| key | gpu.d2h | - | -  | - | 0.80 | - | - | not_tested |
| important | app.compile | 7.12 | 16.6 seconds | 0.358 | 0.50 | 0.27–0.92 | - | fail ~noisy |
| important | app.untar | 20.6 | 6.86 seconds | 3.14 | 0.50 | 1.36–3.31 | - | pass ~noisy |
| important | app.gzip | 2.44 | 4 seconds | 0.579 | 0.50 | 0.19–0.97 | - | pass ~noisy |
| important | fs.metadata | 15,654 | 11,422 ops/s | 0.679 | 0.50 | 0.53–1.31 | - | pass ~noisy |
| important | disk.bind_write | 93.2 | 90.2 MB/s | 0.995 | 0.50 | 0.74–1.17 | - | pass ~noisy |
| important | disk.bind_read | 3,396 | 1,096 MB/s | 0.492 | 0.50 | 0.02–1.78 | - | fail ~noisy |
| important | fs.bind_metadata | - | -  | - | 0.50 | - | - | fail (RuntimeError: docker run --rm --name: ti) |
| important | net.publish_rr | 20,975 | 7,702 round trips/s | 0.375 | 0.50 | 0.33–0.47 | - | fail ~noisy |
| important | net.bridge_rr | 43,812 | 7,711 round trips/s | 0.177 | 0.50 | 0.11–0.22 | - | fail ~noisy |
| important | docker.run | ref 0.4 | 5.55 seconds | 0.0721 | 0.50 | - | - | fail ~noisy |
| important | docker.exec | ref 0.08 | 0.322 seconds | 0.248 | 0.50 | - | - | fail |
| important | docker.build_cached | ref 0.6 | 4.7 seconds | 0.128 | 0.50 | - | -76% | fail ~noisy |
| important | docker.lifecycle | ref 1.2 | 3.57 seconds | 0.336 | 0.50 | - | - | fail ~noisy |
| other | micro.syscall | 1,959,867 | 199,675 ops/s | 0.114 | 0.20 | 0.10–0.16 | - | fail ~noisy |
| other | micro.forkexec | 3,069 | 342.1 ops/s | 0.108 | 0.20 | 0.09–0.12 | -24% | fail ~noisy |
| other | micro.mmap | 111,744 | 5,865 ops/s | 0.0437 | 0.20 | 0.03–0.10 | - | fail ~noisy |
| other | micro.clock | 29,085,558 | 24,233,510 ops/s | 0.845 | 0.20 | 0.58–1.20 | +33% | pass ~noisy |
| other | micro.thread | 20,318 | 4,769 ops/s | 0.251 | 0.20 | 0.08–1.27 | - | pass ~noisy |
| other | micro.pipe | 111,168 | 11,229 round trips/s | 0.105 | 0.20 | 0.06–0.15 | - | fail ~noisy |
| other | micro.signal | 369,069 | 54,114 ops/s | 0.145 | 0.20 | 0.07–0.16 | - | fail ~noisy |
