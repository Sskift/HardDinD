#!/usr/bin/env python3
"""Record syscall availability and stress the kernel's actual rendezvous protocol."""
import argparse
import hashlib
import json
import os
import pathlib
import platform
import subprocess

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
a = p.parse_args()
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
version = json.loads((repo / "sources.lock.json").read_text())["linux"]["version"]
header = work / f"linux-{version}/arch/um/include/shared/skas/stub-futex.h"
if not header.exists():
    p.error("build the patched kernel first")
output = work / "results"
output.mkdir(exist_ok=True)
report = {"kernel_release": platform.release(), "architecture": platform.machine(),
          "euid": os.geteuid(),
          "protocol_sha256": hashlib.sha256(header.read_bytes()).hexdigest(),
          "namespaces": [], "handoff": []}
report["host_process_status"] = {
    key: value.strip() for key, value in
    (line.split(":", 1) for line in pathlib.Path("/proc/self/status").read_text().splitlines())
    if key in {"CapEff", "CapBnd", "Seccomp", "Seccomp_filters", "NoNewPrivs"}}
for name in ("namespace_probe", "handoff_stress"):
    binary = work / name.replace("_", "-")
    subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-I", str(header.parent), str(repo / "src" / (name + ".c")),
                    "-o", str(binary)], check=True)
    arguments = [[]] if name == "namespace_probe" else [
        [str(mode), "100000", str(spin)] for mode in (0, 1) for spin in (0, 256, 4096)]
    for args in arguments:
        proc = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=35)
        if proc.returncode:
            raise SystemExit(f"{name} failed ({proc.returncode}): {proc.stderr}")
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        if name == "namespace_probe":
            if len(rows) != 24:
                raise SystemExit("incomplete namespace probe")
            report["namespaces"] = rows
        else:
            if len(rows) != 1 or rows[0].get("verified") is not True:
                raise SystemExit("incomplete handoff stress")
            report["handoff"].extend(rows)
report["complete"] = True
report["product_acceptance"] = False
(output / "host-probes.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
