"""Locations and defaults. Every value can be overridden by an environment variable."""
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

# Local side (developer machine) -> Starpub. Always go through the ssh alias so
# the ControlMaster connection is reused; the jump host bans new-connection bursts.
REMOTE = os.environ.get('HD_REMOTE', 'starpub-root')
REMOTE_ROOT = os.environ.get('HD_REMOTE_ROOT', '/root/harddind-rebuild/hd')

# Remote side layout (also valid on any Linux host that runs the pipeline).
ROOT = pathlib.Path(os.environ.get('HD_ROOT', REMOTE_ROOT))
WORK = ROOT / 'work'          # build scratch: downloads, kernel source/objects, toolchains
STORE = ROOT / 'store'        # content-addressed build outputs, immutable once complete
RUNS = ROOT / 'runs'          # one directory per test/bench run (raw logs stay here)
INSTANCES = ROOT / 'instances'
LOCKS = ROOT / 'locks'

JOBS = int(os.environ.get('HD_JOBS', '12'))
RUST_TOOLCHAIN = os.environ.get('HD_RUST_TOOLCHAIN', str(ROOT / 'toolchains/rust-1.98.1'))
# Host CPUs used for measurements; native and guest runs are pinned to the same set.
BENCH_CPUS = os.environ.get('HD_BENCH_CPUS', '8-11')
GUEST_CPUS = int(os.environ.get('HD_GUEST_CPUS', '4'))
GUEST_MEMORY = os.environ.get('HD_GUEST_MEMORY', '4G')
