#!/usr/bin/env python3
"""Owner-run guest harness for experiment 0018.

Boots one UML guest with the FP suite disk and classifies the run strictly:

  pass       : clean shutdown, every probe case emitted status "pass"
  incomplete : one or more cases emitted status "skip" (probe exit code 2)
  fail       : any case failed, guest crashed, kernel binary changed on disk,
               or the run TIMED OUT (a timeout is never a pass)

Negative/skip evidence is preserved as JSON+log and returned as incomplete.
This script does NOT run unless explicitly invoked; it performs no host
performance interpretation. Builds are expected to be done already; it takes
a shared flock on the work lock so it never races a kernel build.

Usage:
  fp-run-core.py <work> --kernel <linux> --disk <fp.ext4>
      [--cpus N] [--lazy] [--parallel] [--timeout S] [--result-name NAME]
"""
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

CASES = {
    "gp-syscall", "fp-preserve", "fp-signal-preserve", "fp-signal-edit",
    "gp-signal-edit", "fp-nested", "frame-churn", "thread-exit-churn",
    "runner-lifetime", "fork-fp", "exec-reset", "ptrace-gp-fp",
}
# fp-threads appears once per worker count; accept either cardinality.
CASE_PREFIX = ("fp-threads",)

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--kernel", required=True, type=pathlib.Path)
p.add_argument("--disk", required=True, type=pathlib.Path)
p.add_argument("--cpus", type=int, default=4)
p.add_argument("--lazy", action="store_true", help="append lazyxstate=1")
p.add_argument("--parallel", action="store_true", help="append seccomp_threads=1")
p.add_argument("--timeout", type=int, default=240)
p.add_argument("--result-name", default=None)
a = p.parse_args()
if not 1 <= a.cpus <= 64:
    p.error("cpus must be 1..64")

work = a.work.resolve()
(work / "results").mkdir(parents=True, exist_ok=True)
(work / "tmp").mkdir(exist_ok=True)
with (work / "kernel-build.lock").open("a") as build_lock:
    fcntl.flock(build_lock, fcntl.LOCK_SH)
    kernel = a.kernel.resolve()
    disk = a.disk.resolve()
    tag = f"fp-{a.cpus}cpu" + ("-parallel" if a.parallel else "") + \
          ("-lazyxstate" if a.lazy else "-eager")
    name = a.result_name or tag
    if any((work / "results" / (name + ext)).exists()
           for ext in (".json", ".log")):
        sys.exit("refusing to overwrite existing evidence: " + name)
    log = work / "results" / (name + ".log")
    cow = work / "tmp" / ("fp-" + uuid.uuid4().hex + ".cow")
    cmd = [str(kernel), "umid=harddind-" + uuid.uuid4().hex[:12],
           "mem=2G", f"ncpus={a.cpus}", "seccomp=on",
           f"ubd0={cow},{disk}", "root=/dev/ubda", "rw",
           "init=/sbin/harddind-fp-init",
           "con=null", "con0=null,fd:1", "ssl=null", "panic=1"]
    if a.parallel:
        cmd.append("seccomp_threads=1")
    if a.lazy:
        cmd.append("lazyxstate=1")
    kernel_sha = hashlib.sha256(kernel.read_bytes()).hexdigest()
    probe_sha = "unknown"
    started = time.monotonic()
    timed_out = False
    with log.open("w") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT,
                                env={**os.environ,
                                     "TMPDIR": str(work / "tmp")},
                                start_new_session=True)
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
    for line in text.splitlines():
        if line.startswith("HARDDIND_FP_PROBE_SHA256="):
            probe_sha = line.split("=", 1)[1].strip()

rows = []
for line in text.splitlines():
    if line.startswith('{"test":"fp_probe"'):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
seen = {}
for r in rows:
    seen.setdefault(r.get("case"), []).append(r.get("status"))
missing = sorted(c for c in CASES if c not in seen)
thread_rows = sum(1 for c in seen if c.startswith(CASE_PREFIX))
failed = sorted(c for c, v in seen.items() if "fail" in v)
skipped = sorted(c for c, v in seen.items() if "skip" in v)
end_marker = "HARDDIND_FP_END status=0" in text
incomplete = bool(skipped) or not end_marker and not failed
if timed_out:
    verdict = "fail"
    incomplete = False
elif failed or rc != 0 or missing or thread_rows < 2:
    verdict = "fail"
elif skipped:
    verdict = "incomplete"
elif end_marker and rc == 0:
    verdict = "pass"
else:
    verdict = "incomplete"

report = {
    "kind": "guest_fp_regression",
    "verdict": verdict,
    "kernel": str(kernel),
    "kernel_sha256": kernel_sha,
    "kernel_unchanged":
        hashlib.sha256(kernel.read_bytes()).hexdigest() == kernel_sha,
    "disk": str(disk),
    "probe_sha256": probe_sha,
    "cpus": a.cpus, "parallel": a.parallel, "lazyxstate": a.lazy,
    "exit_code": rc, "timed_out": timed_out,
    "wall_seconds": round(time.monotonic() - started, 3),
    "cases_seen": {c: seen[c] for c in sorted(seen)},
    "missing_cases": missing, "failed_cases": failed,
    "skipped_cases": skipped,
    "end_marker_status0": end_marker,
    "log": str(log),
    "note": "pass requires all cases; skip is incomplete; timeout is fail",
}
(work / "results" / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
sys.exit({"pass": 0, "incomplete": 2, "fail": 1}[verdict])
