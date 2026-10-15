#!/usr/bin/env python3
"""Validate untraced comparisons and distinguish trapped from host syscalls."""
import argparse
import collections
import gzip
import json
import pathlib
import re
import statistics


def parse_trace(lines):
    phases, active = [], None
    for lineno, line in enumerate(lines, 1):
        # The guest write has a newline. UML's subsequent console write of the
        # same marker does not; counting both would invent duplicate phases.
        begin = re.search(r'PROFILE_BEGIN case=(\w+) repeat=(\d+) iterations=(\d+)\\n', line)
        if begin:
            if active is not None:
                raise ValueError("overlapping trace phases")
            active = {"case": begin[1], "repeat": int(begin[2]), "iterations": int(begin[3]),
                      "first_line": lineno, "entries": collections.Counter(),
                      "traps": collections.Counter(), "signals": collections.Counter()}
            continue
        if 'PROFILE_END ' in line and '\\n"' in line:
            if active is None:
                raise ValueError("end without begin")
            end = json.loads(re.search(r'PROFILE_END (.*)\\n"', line)[1].replace('\\"', '"'))
            if (end["case"] != active["case"] or end["repeat"] != active["repeat"]
                    or end["iterations"] != active["iterations"] or end.get("verified") is not True):
                raise ValueError("wrong phase end")
            phases.append({"case": active["case"], "iterations": active["iterations"],
                           "first_line": active["first_line"], "last_line": lineno,
                           "host_syscalls": dict(active["entries"] - active["traps"]),
                           "trapped_guest_syscalls": dict(active["traps"]),
                           "signals": dict(active["signals"])})
            active = None
            continue
        if active is None:
            continue
        # Count entries once, not '<... NAME resumed>' lines. strace displays
        # seccomp-trapped guest syscalls too; remove those from host counts.
        call = re.match(r'\d+ +\d+:\d+:\d+\.\d+ (\w+)\(', line)
        if call:
            active["entries"][call[1]] += 1
        sig = re.search(r'--- (SIG\w+)', line)
        if sig:
            active["signals"][sig[1]] += 1
        trap = re.search(r'si_syscall=__NR_(\w+)', line)
        if trap:
            active["traps"][trap[1]] += 1
    if active is not None:
        raise ValueError("incomplete trace")
    return phases


def analyze(directory):
    files = sorted(directory.glob("profile-time-*.json"), key=lambda f: int(f.name.split("-")[2]))
    order = [("native", 256), ("trap", 256), ("guest", 0), ("guest", 256), ("guest", 4096),
             ("guest", 4096), ("guest", 256), ("guest", 0), ("trap", 256), ("native", 256)]
    if len(files) != len(order):
        raise ValueError("incomplete comparison")
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    reports, common_hash = [], None
    for file, expected in zip(files, order):
        d = json.loads(file.read_text())
        if not d["passed"] or not d["timings_usable"] or (d["mode"], d["spin"]) != expected:
            raise ValueError("failed or mismatched measurement")
        if common_hash is None:
            common_hash = d["sha256"]
        if d["sha256"] != common_hash or d["host_cpu_affinity"] != [0, 1, 2, 3]:
            raise ValueError("different binaries, disk or affinity")
        mode = d["mode"] + (f'-{d["spin"]}' if d["mode"] == "guest" else "")
        for row in d["measurements"]:
            groups[row["case"]][mode].append(row["seconds"] * 1e6 / row["iterations"])
        stat = [{k: int(v) for k, v in (line.split() for line in d["host_" + when]["cpu_stat"].splitlines())}
                for when in ("before", "after")]
        reports.append({"file": file.name, "mode": mode, "host_cpu_seconds": d["host_cpu_seconds"],
                        "voluntary_context_switches": d["voluntary_context_switches"],
                        "involuntary_context_switches": d["involuntary_context_switches"],
                        "cgroup_throttled_usec_delta": stat[1]["throttled_usec"] - stat[0]["throttled_usec"],
                        "cgroup_throttled_periods_delta": stat[1]["nr_throttled"] - stat[0]["nr_throttled"]})
    cases = []
    for case, modes in groups.items():
        wanted = {"native", "guest-0", "guest-256", "guest-4096"} | ({"trap"} if case == "getpid" else set())
        if set(modes) != wanted or any(len(values) != 6 for values in modes.values()):
            raise ValueError("incomplete case")
        cases.append({"case": case, "microseconds_per_iteration": {
            mode: {"median": statistics.median(values), "min": min(values), "max": max(values), "samples": values}
            for mode, values in modes.items()}})
    trace_path = directory / "profile-trace-256.strace.gz"
    with gzip.open(trace_path, "rt") as f:
        phases = parse_trace(f)
    if {r["case"] for r in phases} != set(groups) or len(phases) != len(groups):
        raise ValueError("missing trace phases")
    trace_report = json.loads((directory / "profile-trace-256.json").read_text())
    if not trace_report["passed"] or trace_report["timings_usable"] or trace_report["sha256"] != common_hash:
        raise ValueError("invalid trace run")
    irq_rows = [json.loads(line) for line in (directory / "profile-irqcost.jsonl").read_text().splitlines()]
    if (len(irq_rows) != 6 or {row["repeat"] for row in irq_rows} != set(range(6))
            or any(row.get("verified") is not True or row["iterations"] != 100000 for row in irq_rows)):
        raise ValueError("incomplete host IRQ reference")
    irq_values = [row["seconds"] * 1e6 / row["iterations"] for row in irq_rows]
    return {"kind": "diagnosis_not_acceptance", "complete": True, "sha256": common_hash,
            "order": reports, "cases": cases, "trace_phases": phases,
            "host_irq_pair_microseconds": {"median": statistics.median(irq_values), "samples": irq_values},
            "limitations": ["strace changes scheduling, futex behavior, signal counts and time; use only as path/count evidence",
                            "phase boundaries include marker completion, clock calls and timer activity",
                            "trap reference omits UML, and its time cannot be subtracted to give an exact component percentage",
                            "six samples per case and mode, on pinned but non-exclusive CPUs",
                            "microbenchmarks only; no Docker/GPU/full performance acceptance"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("directory", type=pathlib.Path)
    a = p.parse_args()
    data = analyze(a.directory)
    (a.directory / "analysis.json").write_text(json.dumps(data, indent=2) + "\n")
    for row in data["cases"]:
        print(row["case"], {k: round(v["median"], 4) for k, v in row["microseconds_per_iteration"].items()})
