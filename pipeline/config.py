"""Locations and defaults. Every value can be overridden by an environment variable."""
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

# Local side (developer machine) -> Starpub. The -mux alias only reuses the
# resident ControlMaster and fails closed when it is down: the jump host bans
# source IPs that open new connections too fast, and ./hd is automation.
REMOTE = os.environ.get('HD_REMOTE', 'starpub-root-mux')
REMOTE_ROOT = os.environ.get('HD_REMOTE_ROOT', '/root/harddind-rebuild/hd')
# Multiplexing master that every remote connection must reuse; empty disables the check.
JUMP = os.environ.get('HD_JUMP', 'star-jump')

# Remote side layout (also valid on any Linux host that runs the pipeline).
ROOT = pathlib.Path(os.environ.get('HD_ROOT', REMOTE_ROOT))
WORK = ROOT / 'work'          # build scratch: downloads, kernel source/objects, toolchains
STORE = ROOT / 'store'        # content-addressed build outputs, immutable once complete
RUNS = ROOT / 'runs'          # one directory per test/bench run (raw logs stay here)
INSTANCES = ROOT / 'instances'
LOCKS = ROOT / 'locks'

JOBS = int(os.environ.get('HD_JOBS', '12'))
RUST_TOOLCHAIN = os.environ.get('HD_RUST_TOOLCHAIN', str(ROOT / 'toolchains/rust-1.98.1'))
# Host CPUs for measurements; native and guest runs are pinned to the same set.
# 'auto' picks the quietest physical cores at the start of each run.
BENCH_CPUS = os.environ.get('HD_BENCH_CPUS', 'auto')
GUEST_CPUS = int(os.environ.get('HD_GUEST_CPUS', '4'))
GUEST_MEMORY = os.environ.get('HD_GUEST_MEMORY', '4G')
# Seconds a bench may wait for the outer container to stop being CPU-throttled.
QUIET_WAIT = float(os.environ.get('HD_QUIET_WAIT', '2700'))
