#!/usr/bin/env python3
"""Stress the exact deferred-IPI protocol header on one and several host CPUs."""
import argparse
import fcntl
import hashlib
import json
import pathlib
import subprocess

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--name", default="ipi-host-probes")
a = p.parse_args()
if not a.name or not all(c.isalnum() or c in "-_" for c in a.name):
    p.error("invalid name")
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
version = json.loads((repo / "sources.lock.json").read_text())["linux"]["version"]
header = work / f"linux-{version}/arch/um/include/shared/ipi-defer.h"
source = repo / "src/ipi_stress.c"
lock = (work / "kernel-build.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
binary = work / "ipi-stress"
subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(header.parent),
                str(source), "-o", str(binary)], check=True)
sha = lambda file: hashlib.sha256(file.read_bytes()).hexdigest()
report = {"kind": "deferred_ipi_protocol_only", "source_sha256": sha(source),
          "header_sha256": sha(header), "binary_sha256": sha(binary), "runs": [], "product_acceptance": False, "complete": False}
output = work / "results" / (a.name + ".json")
if output.exists():
    p.error("refusing to overwrite evidence")
def save():
    output.write_text(json.dumps(report, indent=2) + "\n")
save()
for cpus in ("0", "0,1,2,3", "0,1,2,3,4,5,6,7"):
    result = subprocess.run(["taskset", "-c", cpus, str(binary)], capture_output=True, text=True, timeout=75)
    if result.returncode:
        report["failure"] = {"host_cpus": cpus, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        save()
        raise RuntimeError(f"IPI stress failed on CPUs {cpus}: {result.returncode}: {result.stderr}")
    row = json.loads(result.stdout)
    if row.get("verified") is not True or row["signals"] != 400000 or row["queue_items"] != 400000 or row["drain_batches"] < 100:
        raise RuntimeError("incomplete stress evidence")
    report["runs"].append({"host_cpus": cpus, **row})
    save()
report["complete"] = True
save()
print(json.dumps(report, indent=2))
