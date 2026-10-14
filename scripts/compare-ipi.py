#!/usr/bin/env python3
"""Native / previous kernel / same-kernel feature off/on, in mirrored order."""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--baseline", type=pathlib.Path, required=True)
p.add_argument("--kernel", type=pathlib.Path, required=True)
p.add_argument("--name", default="ipi")
p.add_argument("--feature", choices=["defer-ipi", "mprotect-sync"], default="defer-ipi")
a = p.parse_args()
if not a.name or not all(c.isalnum() or c in "-_" for c in a.name):
    p.error("invalid name")
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
lock = (work / "kernel-build.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_SH)
os.sched_setaffinity(0, [0, 1, 2, 3])
out = work / "results"
paths = {"baseline": a.baseline.resolve(), "kernel": a.kernel.resolve(),
         "core": work / "dispatch-root/corebench", "dispatch": work / "dispatch-root/dispatchbench"}
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
hashes = {key: sha(path) for key, path in paths.items()}
cases = [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1), ("processes", 4),
         ("identity", 1), ("fdio", 1), ("mapping", 1), ("thread_syscalls", 4), ("isolation", 1), ("parallel_exec", 4)]
order = ["native", "baseline", "off", "on", "on", "off", "baseline", "native"]
groups = {mode: [] for mode in set(order)}
evidence, failures = [], []
def host_state():
    return {"time_unix": time.time(), **{name: pathlib.Path(path).read_text() for name, path in {
        "cpu_stat": "/sys/fs/cgroup/cpu.stat", "cpu_pressure": "/sys/fs/cgroup/cpu.pressure",
        "loadavg": "/proc/loadavg", "memory_events": "/sys/fs/cgroup/memory.events"}.items()}}

for index, mode in enumerate(order):
    name = f"{a.name}-{index}-{mode}"
    file = out / (name + ".json")
    if file.exists() or (out / (name + ".log")).exists():
        raise RuntimeError("refusing to overwrite evidence")
    host_before = host_state()
    if mode == "native":
        rows = []
        for case_index, (test, workers) in enumerate(cases):
            for _ in range(3):
                cmd = [str(paths["core"]), test, str(workers)] if case_index < 6 else [str(paths["dispatch"]), test]
                row = json.loads(subprocess.check_output(cmd, text=True, timeout=90))
                if (row["test"], row["workers"]) != (test, workers) or (case_index >= 6 and row.get("verified") is not True):
                    raise RuntimeError("invalid native result")
                rows.append(row)
        file.write_text(json.dumps({"mode": mode, "measurements": rows, "sha256": hashes,
                                    "host_cpu_affinity": sorted(os.sched_getaffinity(0))}, indent=2) + "\n")
    else:
        kernel = paths["baseline"] if mode == "baseline" else paths["kernel"]
        cmd = [sys.executable, str(repo / "scripts/run-core.py"), str(work), "--kernel", str(kernel),
               "--threads-experiment", "--broker-cache", "--dispatch-bench", "--disk", str(work / "dispatch.ext4"),
               "--spin", "256", "--timeout", "300", "--result-name", name]
        if a.feature == "mprotect-sync":
            # Compare against the previous IPI+idle kernel with IPI enabled.
            cmd += ["--defer-ipi"]
        if mode == "on":
            cmd += ["--" + a.feature]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL)
        report = json.loads(file.read_text())
        if (report["kernel_sha256"] != hashes["baseline" if mode == "baseline" else "kernel"]
                or report["dispatch_benchmark_sha256"] != hashes["dispatch"] or report["benchmark_sha256"] != hashes["core"]):
            raise RuntimeError("mismatched hashes")
        if proc.returncode or not report["passed"]:
            failures.append(file.name)
            rows = []
        else:
            rows = report["measurements"]
    groups[mode].extend(rows)
    evidence.append({"mode": mode, "file": file.name, "host_before": host_before, "host_after": host_state()})
    print(name + (": FAILED" if file.name in failures else ": complete"), flush=True)
if hashes != {key: sha(path) for key, path in paths.items()}:
    raise RuntimeError("input changed during comparison")
comparison = []
for test, workers in cases:
    samples = {mode: [r["seconds"] for r in rows if (r["test"], r["workers"]) == (test, workers)] for mode, rows in groups.items()}
    if any(len(v) > 6 or any(t <= 0 for t in v) for v in samples.values()):
        raise RuntimeError("invalid samples")
    median = {m: statistics.median(v) if len(v) == 6 else None for m, v in samples.items()}
    comparison.append({"test": test, "workers": workers, "median_seconds": median, "samples_seconds": samples,
                       "off_over_on": median["off"] / median["on"] if median["off"] and median["on"] else None,
                       "on_over_native": median["on"] / median["native"] if median["on"] and median["native"] else None})
complete = not failures and all(all(value is not None for value in row["median_seconds"].values()) for row in comparison)
summary = {"kind": a.feature.replace("-", "_") + "_microbench_not_acceptance", "feature": a.feature,
           "comparison_complete": complete, "failed_runs": failures,
           "sha256": hashes, "order": evidence, "host_cpu_affinity": sorted(os.sched_getaffinity(0)),
           "comparison": comparison, "full_performance_acceptance": False, "threshold": None}
(out / (a.name + "-comparison.json")).write_text(json.dumps(summary, indent=2) + "\n")
raise SystemExit(0 if complete else 1)
