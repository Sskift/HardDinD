#!/usr/bin/env python3
"""Serial, CPU-pinned A/B/B/A for parallel UML runners; not product acceptance."""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import statistics
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--kernel", type=pathlib.Path)
p.add_argument("--cpus", help="Four distinct host CPUs, comma-separated; default selects distinct physical cores")
a = p.parse_args()
work = a.work.resolve()
# Builders take an exclusive lock; nested runners may share this read lock.
# Keep the descriptor alive for the complete run, including native baselines.
build_lock = (work / "kernel-build.lock").open("a")
fcntl.flock(build_lock, fcntl.LOCK_SH)
kernel = a.kernel.resolve() if a.kernel else work / "thread-build/linux"
repo = pathlib.Path(__file__).resolve().parents[1]
allowed = os.sched_getaffinity(0)
if a.cpus:
    cpus = [int(c) for c in a.cpus.split(",")]
else:
    cpus, physical = [], set()
    for c in sorted(allowed):
        topology = pathlib.Path(f"/sys/devices/system/cpu/cpu{c}/topology")
        key = ((topology / "physical_package_id").read_text().strip(),
               (topology / "core_id").read_text().strip())
        if key not in physical:
            physical.add(key)
            cpus.append(c)
        if len(cpus) == 4:
            break
if len(cpus) != 4 or len(set(cpus)) != 4 or not set(cpus) <= allowed:
    p.error("need four distinct allowed CPUs")
os.sched_setaffinity(0, cpus)
results = work / "results"
results.mkdir(exist_ok=True)
binary = work / "probe-root/corebench"
hash_before = hashlib.sha256(kernel.read_bytes()).hexdigest()
bench_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
cases = [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1), ("processes", 4)]
groups = {name: [] for name in ("native", "legacy", "parallel")}
runs = []
for index, mode in enumerate(["native", "legacy", "parallel", "parallel", "legacy", "native"]):
    name = f"threads-ab-{index}-{mode}"
    if mode == "native":
        rows = []
        for test, workers in cases:
            for _ in range(3):
                row = json.loads(subprocess.check_output([str(binary), test, str(workers)], text=True, timeout=60))
                if (row["test"], row["workers"]) != (test, workers):
                    raise RuntimeError("native benchmark output mismatch")
                rows.append(row)
        result = {"kind": "native_microbench", "benchmark_sha256": bench_hash,
                  "host_cpu_affinity": cpus, "measurements": rows}
        (results / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    else:
        cmd = [sys.executable, str(repo / "scripts/run-core.py"), str(work), "--kernel", str(kernel),
               "--disk", str(work / "probe.ext4"), "--spin", "256",
               "--timeout", "240", "--result-name", name]
        if mode == "parallel":
            cmd.append("--threads-experiment")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        result = json.loads((results / (name + ".json")).read_text())
        if not result["passed"] or result["kernel_sha256"] != hash_before or result["benchmark_sha256"] != bench_hash:
            raise RuntimeError("incomplete or changed kernel/benchmark evidence")
        rows = result["measurements"]
    groups[mode] += rows
    runs.append({"mode": mode, "evidence": name + ".json"})
    print(f"{name}: complete", flush=True)
if hashlib.sha256(kernel.read_bytes()).hexdigest() != hash_before:
    raise RuntimeError("kernel changed during comparison")
summary = []
for test, workers in cases:
    medians = {}
    for mode, data in groups.items():
        values = [r["seconds"] for r in data if (r["test"], r["workers"]) == (test, workers)]
        if len(values) != 6 or not all(v > 0 for v in values):
            raise RuntimeError("missing or invalid samples")
        medians[mode] = statistics.median(values)
    summary.append({"test": test, "workers": workers, "median_seconds": medians,
                    "parallel_over_native": medians["parallel"] / medians["native"],
                    "legacy_over_parallel": medians["legacy"] / medians["parallel"]})
report = {"kind": "shared_vm_runners_microbench_only", "kernel_sha256": hash_before,
          "benchmark_sha256": bench_hash, "host_cpu_affinity": cpus,
          "samples_per_case_per_mode": 6, "order": runs, "comparison": summary,
          "full_performance_acceptance": False, "threshold": None,
          "limitations": ["CPU affinity is shared but CPUs are not exclusively reserved", "no storage or network workload comparison"]}
(results / "threads-comparison.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
