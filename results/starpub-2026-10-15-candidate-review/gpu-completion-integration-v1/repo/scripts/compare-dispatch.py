#!/usr/bin/env python3
"""Pinned native / old parallel / waiter / broker-cache comparison, ABCCBA."""
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
p.add_argument("--before", type=pathlib.Path, required=True)
p.add_argument("--handoff", type=pathlib.Path, required=True)
p.add_argument("--after", type=pathlib.Path, required=True)
p.add_argument("--cpus", default="0,1,2,3")
p.add_argument("--spin", type=int, default=256)
p.add_argument("--name", default="dispatch")
a = p.parse_args()
if not a.name or not all(c.isalnum() or c in "-_" for c in a.name):
    p.error("invalid name")
if not 0 <= a.spin <= 10000:
    p.error("spin must be 0..10000")
cpus = [int(c) for c in a.cpus.split(",")]
if len(cpus) != 4 or len(set(cpus)) != 4 or not set(cpus) <= os.sched_getaffinity(0):
    p.error("need four distinct allowed host CPUs")
os.sched_setaffinity(0, cpus)
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
lock = (work / "kernel-build.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_SH)
out = work / "results"
out.mkdir(exist_ok=True)
kernels = {"before": a.before.resolve(), "handoff": a.handoff.resolve(), "after": a.after.resolve()}
binaries = {"core": work / "dispatch-root/corebench", "dispatch": work / "dispatch-root/dispatchbench"}
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
hashes = {name: sha(path) for name, path in {**kernels, **binaries}.items()}
cases = [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1),
         ("processes", 4), ("identity", 1), ("fdio", 1), ("mapping", 1), ("thread_syscalls", 4), ("isolation", 1), ("parallel_exec", 4)]
groups = {name: [] for name in ["native", *kernels]}
order = ["native", "before", "handoff", "after", "after", "handoff", "before", "native"]
reports = []
failures = []
for index, mode in enumerate(order):
    name = f"{a.name}-{index}-{mode}"
    if mode == "native":
        rows = []
        for case_index, (test, workers) in enumerate(cases):
            for _ in range(3):
                cmd = [str(binaries["core"]), test, str(workers)] if case_index < 6 else [str(binaries["dispatch"]), test]
                row = json.loads(subprocess.check_output(cmd, text=True, timeout=90))
                if (row["test"], row["workers"]) != (test, workers) or (case_index >= 6 and row.get("verified") is not True):
                    raise RuntimeError("invalid native result")
                rows.append(row)
        report = {"kind": "native_microbench", "host_cpu_affinity": cpus,
                  "benchmark_sha256": hashes["core"], "dispatch_benchmark_sha256": hashes["dispatch"],
                  "measurements": rows}
        (out / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    else:
        cmd = [sys.executable, str(repo / "scripts/run-core.py"), str(work), "--kernel", str(kernels[mode]),
               "--threads-experiment", "--dispatch-bench", "--disk", str(work / "dispatch.ext4"),
               "--spin", str(a.spin), "--timeout", "300", "--result-name", name]
        if mode == "after":
            cmd.append("--broker-cache")
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL)
        report = json.loads((out / (name + ".json")).read_text())
        if (report["kernel_sha256"] != hashes[mode]
                or report["benchmark_sha256"] != hashes["core"]
                or report["dispatch_benchmark_sha256"] != hashes["dispatch"]):
            raise RuntimeError("mismatched guest evidence")
        if process.returncode or not report["passed"]:
            failures.append(name + ".json")
            # Keep the raw failed run, but never use its partial timings.
            rows = []
        else:
            rows = report["measurements"]
    groups[mode] += rows
    reports.append({"mode": mode, "evidence": name + ".json"})
    print(f"{name}: {'FAILED' if name + '.json' in failures else 'complete'}", flush=True)
for name, path in {**kernels, **binaries}.items():
    if sha(path) != hashes[name]:
        raise RuntimeError(f"binary changed: {name}")
comparison = []
for test, workers in cases:
    medians, samples = {}, {}
    for mode, rows in groups.items():
        values = [r["seconds"] for r in rows if (r["test"], r["workers"]) == (test, workers)]
        if len(values) > 6 or not all(v > 0 for v in values):
            raise RuntimeError("extra or invalid samples")
        medians[mode] = statistics.median(values) if len(values) == 6 else None
        samples[mode] = values
    comparison.append({"test": test, "workers": workers, "median_seconds": medians,
                       "samples_seconds": samples,
                       "before_over_after": medians["before"] / medians["after"] if medians["before"] and medians["after"] else None,
                       "after_over_native": medians["after"] / medians["native"] if medians["after"] else None})
summary = {"kind": "dispatch_and_broker_microbench_only", "sha256": hashes, "host_cpu_affinity": cpus,
           "spin": a.spin, "samples_per_case_per_mode": 6, "order": reports, "comparison": comparison,
           "comparison_complete": not failures, "failed_runs": failures,
           "full_performance_acceptance": False, "threshold": None,
           "limitations": ["CPUs are pinned, not exclusively reserved", "no storage/network application parity or GPU proof"]}
(out / (a.name + "-comparison.json")).write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
sys.exit(1 if failures else 0)
