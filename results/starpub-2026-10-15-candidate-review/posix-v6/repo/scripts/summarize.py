#!/usr/bin/env python3
"""Summarize paired microbenchmarks without declaring performance equivalence."""
import argparse
import json
import math
import pathlib
import statistics

p = argparse.ArgumentParser()
p.add_argument("results", type=pathlib.Path)
a = p.parse_args()
root = a.results
native = [json.loads(line) for line in (root / "native-core.jsonl").read_text().splitlines()]
profiles = {}
kernel_hashes = set()
benchmark_hashes = set()
for path in sorted(root.glob("*cpu-spin*.json")):
    d = json.loads(path.read_text())
    if not d.get("passed"):
        raise SystemExit(f"incomplete run: {path}")
    kernel_hashes.add(d["kernel_sha256"])
    benchmark_hashes.add(d["benchmark_sha256"])
    profiles[path.stem] = d
if not profiles or len(kernel_hashes) != 1 or len(benchmark_hashes) != 1:
    raise SystemExit("missing or inconsistent benchmark/kernel identities")
native_hashes = {line.split()[0] for line in (root / "benchmark-sha256.txt").read_text().splitlines()}
if native_hashes != benchmark_hashes:
    raise SystemExit("native and guest benchmark binaries differ")

def median(rows, test, workers):
    samples = [r["seconds"] for r in rows if r["test"] == test and r["workers"] == workers]
    if len(samples) != 3 or any(not math.isfinite(x) or x <= 0 for x in samples):
        raise SystemExit(f"invalid samples for {test}/{workers}")
    return statistics.median(samples)

rows = []
for test, workers in [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1), ("processes", 4)]:
    base = median(native, test, workers)
    variants = {}
    for name, d in profiles.items():
        value = median(d["measurements"], test, workers)
        variants[name] = {"seconds": value, "ratio_to_native": value / base}
    rows.append({"test": test, "workers": workers, "native_seconds": base, "profiles": variants})
report = {"kind": "diagnostic_microbench_comparison", "performance_acceptance": "not_established",
          "limitations": ["three repetitions only", "not an upstream native Docker baseline", "not complete workload coverage",
                          "outer process and guest do not have identical resource limits"],
          "kernel_sha256": kernel_hashes.pop(), "benchmark_sha256": benchmark_hashes.pop(),
          "rows": rows,
          "host_cpu_seconds": {name: d["host_cpu_seconds"] for name, d in profiles.items()}}
(root / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
for row in rows:
    print(f'{row["test"]}/{row["workers"]}: native {row["native_seconds"]:.4f}s; ' +
          "; ".join(f'{name} {v["seconds"]:.4f}s ({v["ratio_to_native"]:.1f}x)' for name, v in row["profiles"].items()))
