#!/usr/bin/env python3
"""Run stock Docker in the candidate kernel; preserve failures and omissions."""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import uuid
from result_checks import docker_evidence, broker_cache_evidence, deferred_ipi_evidence, mprotect_evidence
from guest_cleanup import cleanup_group

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--timeout", type=int, default=240)
p.add_argument("--disk", type=pathlib.Path, help="Pristine Docker rootfs; it is opened as a COW backing file")
p.add_argument("--kernel", type=pathlib.Path)
p.add_argument("--threads-experiment", action="store_true")
p.add_argument("--broker-cache", action="store_true")
p.add_argument("--defer-ipi", action="store_true")
p.add_argument("--mprotect-sync", action="store_true")
p.add_argument("--checks", help="Run only check names matching this regex; others stay not_tested")
p.add_argument("--result-name", default="docker")
a = p.parse_args()
if a.mprotect_sync and not a.threads_experiment:
    p.error("mprotect synchronization requires parallel runners")
if a.checks:
    if not re.fullmatch(r"[A-Za-z0-9_|^$().-]+", a.checks):
        p.error("check filter must be a compact regex without whitespace")
    try:
        re.compile(a.checks)
    except re.error as error:
        p.error("invalid check filter: " + str(error))
if a.broker_cache and not a.threads_experiment:
    p.error("broker cache requires parallel runners")
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
if log.exists() or (out / (a.result_name + ".json")).exists():
    p.error("refusing to overwrite evidence")
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
if a.broker_cache:
    cmd.append("seccomp_broker_cache=1")
if a.defer_ipi:
    cmd.append("defer_ipi=1")
if a.mprotect_sync:
    cmd.append("mprotect_sync=1")
if a.checks:
    cmd.append("docker_checks=" + a.checks)
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
        cleanup = cleanup_group(proc.pid)
        cow.unlink(missing_ok=True)
text = log.read_text(errors="replace")
kernel_unchanged = hashlib.sha256(kernel.read_bytes()).hexdigest() == kernel_sha256
evidence = docker_evidence(text, rc, timed_out)
suite_sha = next((line.split("=", 1)[1] for line in text.splitlines()
                  if line.startswith("HARDDIND_SUITE_SHA256=")), None)
expected_suite_sha = hashlib.sha256((pathlib.Path(__file__).resolve().parents[1] / "tests/docker-suite.sh").read_bytes()).hexdigest()
if suite_sha != expected_suite_sha:
    evidence["subset_passed"] = False
    evidence["finished"] = False
cache_stats = broker_cache_evidence(text)
ipi_stats = deferred_ipi_evidence(text)
protect_stats = mprotect_evidence(text)
if a.mprotect_sync and not protect_stats["verified"]:
    evidence["subset_passed"] = False
    evidence["finished"] = False
if a.defer_ipi and not ipi_stats["verified"]:
    evidence["subset_passed"] = False
    evidence["finished"] = False
if a.broker_cache and not cache_stats["verified"]:
    evidence["subset_passed"] = False
    evidence["finished"] = False
if not kernel_unchanged or not cleanup["clean_shutdown"]:
    evidence["subset_passed"] = False
    evidence["finished"] = False
report = {"kind": "docker_subset_in_candidate", **evidence, "exit_code": rc,
          "timed_out": timed_out, "wall_seconds": time.monotonic() - start,
          "disk_mode": "isolated_cow", "threads_experiment": a.threads_experiment,
          "broker_cache": a.broker_cache,
          "broker_cache_stats": cache_stats,
          "defer_ipi": a.defer_ipi, "deferred_ipi_stats": ipi_stats,
          "mprotect_sync": a.mprotect_sync, "mprotect_stats": protect_stats,
          "mapping_semantics_sha256": next((line.split("=", 1)[1] for line in text.splitlines()
                                              if line.startswith("HARDDIND_MAPPING_SHA256=")), None),
          "suite_sha256": suite_sha, "expected_suite_sha256": expected_suite_sha,
          "check_filter": a.checks,
          "host_cpu_affinity": sorted(os.sched_getaffinity(0)),
          "dispatch_benchmark_sha256": next((line.split("=", 1)[1] for line in text.splitlines()
                                              if line.startswith("HARDDIND_DISPATCH_SHA256=")), None),
          "full_acceptance": False,
          "thread_semantics_sha256": next((line.split("=", 1)[1] for line in text.splitlines()
                                           if line.startswith("HARDDIND_THREAD_SHA256=")), None),
          "kernel_sha256": kernel_sha256, "kernel_unchanged": kernel_unchanged}
report["cleanup"] = cleanup
(out / (a.result_name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
if not evidence["finished"]:
    print("\n".join(text.splitlines()[-55:]), file=sys.stderr)
# Deliberately nonzero for not_tested, missing checks, or a GPU failure.
sys.exit(0 if evidence["subset_passed"] else 1)
