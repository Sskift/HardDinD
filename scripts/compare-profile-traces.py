#!/usr/bin/env python3
"""Compare IPI off/on syscall counts; traced elapsed times are never compared."""
import argparse
import gzip
import importlib.util
import json
import pathlib

spec = importlib.util.spec_from_file_location("profile_analysis", pathlib.Path(__file__).with_name("analyze-profile.py"))
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def load(report_path):
    report = json.loads(report_path.read_text())
    if not report["passed"] or report["mode"] != "trace" or report["timings_usable"]:
        raise ValueError("only complete diagnostic traces are comparable")
    trace = report_path.with_suffix(".strace.gz")
    with gzip.open(trace, "rt") as f:
        phases = analysis.parse_trace(f)
    if len(phases) != 10 or {row["case"] for row in phases} != {
        "empty", "getpid", "map_none", "map_1page", "map_16pages", "map_protect",
        "reuse_16pages", "protect_pair", "fork", "exec"
    }:
        raise ValueError("missing or duplicate trace phases")
    return report, {row["case"]: row for row in phases}


def compare(off_path, on_path):
    off, off_phases = load(off_path)
    on, on_phases = load(on_path)
    if off["defer_ipi"] or not on["defer_ipi"] or not on["deferred_ipi_stats"]["verified"]:
        raise ValueError("expected verified IPI off/on traces")
    for key in ("sha256", "host_cpu_affinity", "spin"):
        if off[key] != on[key]:
            raise ValueError("different inputs: " + key)
    rows = []
    for case, before in off_phases.items():
        after = on_phases[case]
        if before["iterations"] != after["iterations"]:
            raise ValueError("different work per phase")
        rows.append({"case": case, "iterations": before["iterations"], "off": before, "on": after})
    return {"kind": "ipi_trace_counts_not_timings", "sha256": on["sha256"],
            "host_cpu_affinity": on["host_cpu_affinity"], "spin": on["spin"],
            "off_report": off_path.name, "on_report": on_path.name, "phases": rows,
            "product_acceptance": False,
            "limitations": ["strace changes scheduling and signal counts; do not infer time percentages",
                            "phase boundaries include marker completion, clocks and timer activity"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("off", type=pathlib.Path)
    p.add_argument("on", type=pathlib.Path)
    p.add_argument("output", type=pathlib.Path)
    a = p.parse_args()
    result = compare(a.off, a.on)
    with a.output.open("x") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
