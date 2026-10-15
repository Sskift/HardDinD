#!/usr/bin/env python3
"""Reproduce the missed wakeup using actual before/after os_idle_sleep bodies."""
import argparse
import fcntl
import hashlib
import json
import pathlib
import subprocess
import tempfile

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--before", type=pathlib.Path, required=True)
p.add_argument("--name", default="ipi-idle-probes")
a = p.parse_args()
if not a.name or not all(c.isalnum() or c in "-_" for c in a.name):
    p.error("invalid result name")
work = a.work.resolve()
repo = pathlib.Path(__file__).resolve().parents[1]
version = json.loads((repo / "sources.lock.json").read_text())["linux"]["version"]
kernel = work / f"linux-{version}"
source = repo / "src/ipi_idle_probe.c"
header = kernel / "arch/um/include/shared/ipi-defer.h"
output = work / "results" / (a.name + ".json")
if output.exists():
    p.error("refusing to overwrite evidence")
lock = (work / "kernel-build.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
sha = lambda data: hashlib.sha256(data).hexdigest()
report = {"kind": "idle_wakeup_regression", "complete": False,
          "source_sha256": sha(source.read_bytes()), "protocol_sha256": sha(header.read_bytes()), "runs": []}
with tempfile.TemporaryDirectory(prefix="ipi-idle-") as tmp:
    tmp = pathlib.Path(tmp)
    for fixed, path in [(False, a.before), (True, kernel / "arch/um/os-Linux/time.c")]:
        content = path.read_text()
        # os_idle_sleep is the final function in both pinned source files.
        body = content[content.index("void os_idle_sleep(void)\n{"):]
        (tmp / "idle-under-test.h").write_text(body)
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(tmp),
                        "-I", str(header.parent), str(source), "-o", str(tmp / "probe")], check=True)
        proc = subprocess.run([str(tmp / "probe"), str(int(fixed))], text=True, capture_output=True, timeout=30)
        row = {"corrected": fixed, "kernel_source_sha256": sha(path.read_bytes()),
               "body_sha256": sha(body.encode()), "exit_code": proc.returncode,
               "stdout": proc.stdout, "stderr": proc.stderr}
        report["runs"].append(row)
        output.write_text(json.dumps(report, indent=2) + "\n")
        if proc.returncode or json.loads(proc.stdout).get("verified") is not True:
            raise RuntimeError("idle wakeup probe failed")
report["complete"] = True
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
