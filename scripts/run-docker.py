#!/usr/bin/env python3
"""Run stock Docker in the candidate kernel; preserve failures and omissions."""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid
from result_checks import docker_evidence

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--timeout", type=int, default=240)
p.add_argument("--disk", type=pathlib.Path, help="Pristine Docker rootfs; it is opened as a COW backing file")
p.add_argument("--kernel", type=pathlib.Path)
p.add_argument("--threads-experiment", action="store_true")
p.add_argument("--result-name", default="docker")
a = p.parse_args()
if not a.result_name or not all(c.isalnum() or c in "-_" for c in a.result_name):
    p.error("invalid result-name")
work = a.work.resolve()
# Builders take an exclusive lock; nested runners may share this read lock.
# Keep the descriptor alive for the complete run, including native baselines.
build_lock = (work / "kernel-build.lock").open("a")
fcntl.flock(build_lock, fcntl.LOCK_SH)
out = work / "results"
out.mkdir(exist_ok=True)
log = out / (a.result_name + ".log")
kernel = a.kernel.resolve() if a.kernel else work / "kernel-build/linux"
tmp = work / "tmp"
tmp.mkdir(exist_ok=True)
cow = tmp / ("docker-" + uuid.uuid4().hex + ".cow")
disk = a.disk.resolve() if a.disk else work / "docker.ext4"
cmd = [str(kernel), "umid=harddind-" + uuid.uuid4().hex[:12],
       "mem=4G", "ncpus=4", "seccomp=on", "seccomp_spin=256",
       f"ubd0={cow},{disk}", "root=/dev/ubda", "rw", "init=/sbin/harddind-docker-init",
       "con=null", "con0=null,fd:1", "ssl=null", "panic=1"]
if a.threads_experiment:
    cmd.append("seccomp_threads=1")
timed_out = False
kernel_sha256 = hashlib.sha256(kernel.read_bytes()).hexdigest()
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
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        cow.unlink(missing_ok=True)
text = log.read_text(errors="replace")
kernel_unchanged = hashlib.sha256(kernel.read_bytes()).hexdigest() == kernel_sha256
evidence = docker_evidence(text, rc, timed_out)
if not kernel_unchanged:
    evidence["subset_passed"] = False
    evidence["finished"] = False
report = {"kind": "docker_subset_in_candidate", **evidence, "exit_code": rc,
          "timed_out": timed_out, "wall_seconds": time.monotonic() - start,
          "disk_mode": "isolated_cow", "threads_experiment": a.threads_experiment,
          "full_acceptance": False,
          "thread_semantics_sha256": next((line.split("=", 1)[1] for line in text.splitlines()
                                           if line.startswith("HARDDIND_THREAD_SHA256=")), None),
          "kernel_sha256": kernel_sha256, "kernel_unchanged": kernel_unchanged}
(out / (a.result_name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
if not evidence["finished"]:
    print("\n".join(text.splitlines()[-55:]), file=sys.stderr)
# Deliberately nonzero for not_tested, missing checks, or a GPU failure.
sys.exit(0 if evidence["subset_passed"] else 1)
