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
import re
import signal
import subprocess
import sys
import time
import uuid

CASES = {
    "gp-syscall", "fp-preserve", "fp-signal-preserve", "fp-signal-edit",
    "gp-signal-edit", "fp-nested", "frame-churn", "thread-exit-churn",
    "runner-lifetime", "fork-fp", "exec-reset", "ptrace-gp-fp",
    "ptrace-prefix-semantics",
}
# fp-threads appears once per worker count; accept either cardinality.
CASE_PREFIX = ("fp-threads",)

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--kernel", type=pathlib.Path)
p.add_argument("--disk", type=pathlib.Path)
p.add_argument("--cpus", type=int, default=4)
p.add_argument("--lazy", action="store_true", help="append lazyxstate=1")
p.add_argument("--parallel", action="store_true", help="append seccomp_threads=1")
p.add_argument("--timeout", type=int, default=240)
p.add_argument("--result-name", default=None)
p.add_argument("--expect-probe-sha", default=None,
               help="required sha256 of the in-guest /fp-probe binary")
p.add_argument("--validate-log-only", default=None, type=pathlib.Path,
               help="offline: validate an existing guest log, launch nothing")
a = p.parse_args()
if not 1 <= a.cpus <= 64:
    p.error("cpus must be 1..64")

work = a.work.resolve()
(work / "results").mkdir(parents=True, exist_ok=True)
(work / "tmp").mkdir(exist_ok=True)

if a.validate_log_only:
    # Offline evidence validation: no guest, no kernel/disk access.
    vlog = a.validate_log_only.read_text(errors="replace")
    vlines = vlog.splitlines()
    v_rows, v_bad = [], []
    for line in vlines:
        if line.startswith('{"test":"fp_probe"'):
            try:
                r = json.loads(line)
                assert isinstance(r.get("case"), str)
                assert r.get("status") in ("pass", "fail", "skip")
                assert isinstance(r.get("ok"), bool)
            except Exception:
                v_bad.append(line[:160]); continue
            v_rows.append(r)
    v_seen = {}
    for r in v_rows:
        v_seen.setdefault(r["case"], []).append(r["status"])
    v_expected = set(CASES) | {"fp-threads-1", "fp-threads-4"}
    v_sha = next((l.split("=", 1)[1] for l in vlines
                  if l.startswith("HARDDIND_FP_PROBE_SHA256=")), "")
    v_rc = [l.split("=", 1)[1] for l in vlines
            if l.startswith("HARDDIND_FP_PROBE_RC=")]
    v_problems = []
    if v_bad:
        v_problems.append(f"malformed_json={len(v_bad)}")
    if any(len(v) != 1 for v in v_seen.values()):
        v_problems.append("duplicate_cases")
    if set(v_seen) - v_expected:
        v_problems.append("unknown_cases")
    if v_expected - set(v_seen):
        v_problems.append("missing_cases")
    if any("fail" in v[0] for v in v_seen.values()):
        v_problems.append("failed_cases")
    if sorted(c for c in v_seen if c.startswith(CASE_PREFIX)) != \
       ["fp-threads-1", "fp-threads-4"]:
        v_problems.append("fp_threads_cardinality")
    if not re.fullmatch(r"[0-9a-f]{64}", v_sha):
        v_problems.append("probe_sha_missing_or_malformed")
    if a.expect_probe_sha and v_sha != a.expect_probe_sha:
        v_problems.append("probe_sha_mismatch")
    if sum(1 for l in vlines if l.strip() == "HARDDIND_FP_BEGIN") != 1:
        v_problems.append("begin_marker")
    if sum(1 for l in vlines if l.strip() == "HARDDIND_FP_END status=0") != 1:
        v_problems.append("end_marker")
    if v_rc != ["0"]:
        v_problems.append("probe_rc_marker")
    verdict = "pass" if not v_problems and not any(
        v[0] == "skip" for v in v_seen.values()) else (
        "incomplete" if not v_problems else "fail")
    print(json.dumps({"kind": "offline_log_validation", "verdict": verdict,
                      "problems": v_problems, "cases": len(v_seen),
                      "probe_sha256": v_sha}, indent=2))
    sys.exit({"pass": 0, "incomplete": 2, "fail": 1}[verdict])
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
    disk_sha = hashlib.sha256(disk.read_bytes()).hexdigest()
    probe_sha = "unknown"
    started = time.monotonic()
    timed_out = False
    group_state = {"killed": False}

    def kill_group(p):
        """Reap the whole guest process group; tolerant of a gone leader."""
        try:
            os.killpg(p.pid, signal.SIGKILL)
            group_state["killed"] = True
        except ProcessLookupError:
            pass
        # Always also signal the leader and reap, including normal exit.
        try:
            os.kill(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            p.wait()
        except Exception:
            pass

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
            kill_group(proc)
            rc = -signal.SIGKILL
        finally:
            if proc.poll() is None:
                kill_group(proc)
            # On a normal leader exit still reap any lingering group members.
            if not group_state["killed"]:
                try:
                    os.killpg(proc.pid, 0)
                    os.killpg(proc.pid, signal.SIGKILL)
                    group_state["killed"] = True
                except ProcessLookupError:
                    pass
            cow.unlink(missing_ok=True)
    kernel_sha_after = hashlib.sha256(kernel.read_bytes()).hexdigest()
    kernel_unchanged = kernel_sha == kernel_sha_after
    text = log.read_text(errors="replace")
    lines = text.splitlines()
    for line in lines:
        if line.startswith("HARDDIND_FP_PROBE_SHA256="):
            probe_sha = line.split("=", 1)[1].strip()

# Exactly-once control markers.
def count_exact(marker):
    return sum(1 for l in lines if l.strip() == marker)

begin_n = count_exact("HARDDIND_FP_BEGIN")
end_n = count_exact("HARDDIND_FP_END status=0")
rc_lines = [l.split("=", 1)[1].strip() for l in lines
            if l.startswith("HARDDIND_FP_PROBE_RC=")]
probe_sha_lines = [l.split("=", 1)[1].strip() for l in lines
                   if l.startswith("HARDDIND_FP_PROBE_SHA256=")]
probe_sha = probe_sha_lines[0] if probe_sha_lines else ""

# Parse every probe JSON line: malformed lines are hard failures.
rows = []
malformed = []
for line in lines:
    if line.startswith('{"test":"fp_probe"'):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(line[:160])
            continue
        if not isinstance(r, dict) or not isinstance(r.get("case"), str) or \
           r.get("status") not in ("pass", "fail", "skip") or \
           not isinstance(r.get("ok"), bool) or \
           r["ok"] != (r["status"] == "pass"):
            malformed.append(line[:160])
        else:
            rows.append(r)
seen = {}
for r in rows:
    seen.setdefault(r["case"], []).append(r["status"])

# Duplicate rows for the same case are failures (every case fires once).
duplicates = sorted(c for c, v in seen.items() if len(v) != 1)
# Any case name outside the known expected set is a failure.
expected = set(CASES) | {"fp-threads-1", "fp-threads-4"}
unknown = sorted(c for c in seen if c not in expected)
missing = sorted(c for c in expected if c not in seen)
failed = sorted(c for c, v in seen.items() if v[0] == "fail")
skipped = sorted(c for c, v in seen.items() if v[0] == "skip")
thread_cases = sorted(c for c in seen if c.startswith(CASE_PREFIX))
sha_ok = bool(re.fullmatch(r"[0-9a-f]{64}", probe_sha))
sha_expected_ok = (a.expect_probe_sha is None or
                   probe_sha == a.expect_probe_sha)
markers_ok = (begin_n == 1 and end_n == 1 and len(rc_lines) == 1 and
              rc_lines == ["0"] and len(probe_sha_lines) == 1)

if timed_out or not kernel_unchanged:
    verdict = "fail"
elif (malformed or duplicates or unknown or missing or failed or
      rc != 0 or not markers_ok or not sha_ok or not sha_expected_ok or
      thread_cases != ["fp-threads-1", "fp-threads-4"]):
    verdict = "fail"
elif skipped:
    verdict = "incomplete"
else:
    verdict = "pass"

report = {
    "kind": "guest_fp_regression",
    "verdict": verdict,
    "kernel": str(kernel),
    "kernel_sha256": kernel_sha,
    "kernel_sha256_after": kernel_sha_after,
    "kernel_unchanged": kernel_unchanged,
    "disk": str(disk),
    "disk_sha256": disk_sha,
    "probe_sha256": probe_sha,
    "probe_sha256_wellformed": sha_ok,
    "probe_sha256_expected": a.expect_probe_sha,
    "probe_sha256_matches": sha_expected_ok,
    "cpus": a.cpus, "parallel": a.parallel, "lazyxstate": a.lazy,
    "exit_code": rc, "timed_out": timed_out,
    "process_group_killed": group_state["killed"],
    "wall_seconds": round(time.monotonic() - started, 3),
    "cases_seen": {c: seen[c] for c in sorted(seen)},
    "missing_cases": missing, "failed_cases": failed,
    "skipped_cases": skipped,
    "duplicate_cases": duplicates, "unknown_cases": unknown,
    "malformed_json_lines": len(malformed),
    "fp_threads_cases": thread_cases,
    "markers": {"begin": begin_n, "end_status0": end_n,
                "probe_rc_lines": rc_lines},
    "log": str(log),
    "note": "pass requires exactly one of each case incl fp-threads-1/4, "
            "well-formed probe hash, exactly-once begin/end/rc markers, "
            "stable kernel; skip is incomplete; timeout is fail",
}
(work / "results" / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
sys.exit({"pass": 0, "incomplete": 2, "fail": 1}[verdict])
