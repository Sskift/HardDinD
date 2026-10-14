#!/usr/bin/env python3
"""Interleave old/new kernels on identical COW disks; do not infer full parity."""
import argparse
import json
import pathlib
import statistics
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--before", type=pathlib.Path, required=True)
a = p.parse_args()
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
reports = {}
for spin in (0, 256, 4096):
    for index, variant in enumerate(("before", "after", "after", "before")):
        name = f"handoff-{variant}-spin{spin}-{index}"
        cmd = [sys.executable, str(repo / "scripts/run-core.py"), str(work),
               "--cpus", "4", "--spin", str(spin), "--result-name", name]
        if variant == "before":
            cmd += ["--kernel", str(a.before.resolve())]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        reports[name] = json.loads((work / "results" / (name + ".json")).read_text())
        print(f"completed {name}", flush=True)
if len({r["benchmark_sha256"] for r in reports.values()}) != 1:
    raise SystemExit("benchmark binary changed during comparison")
summary = {"kind": "interleaved_kernel_microbench", "full_performance_acceptance": False,
           "order_per_spin": ["before", "after", "after", "before"], "results": {}}
for spin in (0, 256, 4096):
    section = {}
    for variant in ("before", "after"):
        selected = [r for name, r in reports.items() if f"-{variant}-spin{spin}-" in name]
        hashes = {r["kernel_sha256"] for r in selected}
        if len(hashes) != 1:
            raise SystemExit("kernel changed during comparison")
        measurements = {}
        for row in selected[0]["measurements"]:
            key = (row["test"], row["workers"])
            values = [x["seconds"] for r in selected for x in r["measurements"]
                      if (x["test"], x["workers"]) == key]
            if len(values) != 6:
                raise SystemExit("incomplete paired measurements")
            measurements[f"{key[0]}_{key[1]}"] = statistics.median(values)
        section[variant] = {"kernel_sha256": hashes.pop(), "median_seconds": measurements,
                            "mean_host_cpu_seconds_per_boot": statistics.mean(r["host_cpu_seconds"] for r in selected)}
    section["after_over_before"] = {
        name: value / section["before"]["median_seconds"][name]
        for name, value in section["after"]["median_seconds"].items()}
    summary["results"][str(spin)] = section
(work / "results/handoff-comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
