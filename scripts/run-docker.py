#!/usr/bin/env python3
"""Run stock Docker in the candidate kernel; preserve failures and omissions."""
import argparse
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--timeout", type=int, default=240)
a = p.parse_args()
work = a.work.resolve()
out = work / "results"
out.mkdir(exist_ok=True)
log = out / "docker.log"
tmp = work / "tmp"
tmp.mkdir(exist_ok=True)
cmd = [str(work / "kernel-build/linux"), "umid=harddind-" + uuid.uuid4().hex[:12],
       "mem=4G", "ncpus=4", "seccomp=on", "seccomp_spin=256",
       f"ubd0={work / 'docker.ext4'}", "root=/dev/ubda", "rw", "init=/sbin/harddind-docker-init",
       "con=null", "con0=null,fd:1", "ssl=null", "panic=1"]
timed_out = False
start = time.monotonic()
with log.open("w") as f:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                            env={**os.environ, "TMPDIR": str(tmp)}, start_new_session=True)
    try:
        rc = proc.wait(timeout=a.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        rc = proc.wait()
text = log.read_text(errors="replace")
rows = []
for line in text.splitlines():
    if line.startswith("HARDDIND_RESULT "):
        rows.append(json.loads(line.removeprefix("HARDDIND_RESULT ")))
finished = not timed_out and rc == 0 and "HARDDIND_DOCKER_END status=" in text
report = {"kind": "docker_subset_in_candidate", "finished": finished, "exit_code": rc,
          "timed_out": timed_out, "wall_seconds": time.monotonic() - start,
          "checks": rows, "full_acceptance": False,
          "kernel_sha256": hashlib.sha256((work / "kernel-build/linux").read_bytes()).hexdigest()}
(out / "docker.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
if not finished:
    print("\n".join(text.splitlines()[-55:]), file=sys.stderr)
# Deliberately nonzero for not_tested, missing checks, or a GPU failure.
sys.exit(0 if finished and rows and all(r["status"] == "pass" for r in rows) else 1)
