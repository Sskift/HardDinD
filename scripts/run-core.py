#!/usr/bin/env python3
"""Boot isolated UML tests; boot success alone is never a passing experiment."""
import argparse
import hashlib
import json
import os
import pathlib
import resource
import signal
import subprocess
import sys
import time
import uuid

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--mode", choices=["ptrace", "seccomp"], default="seccomp")
p.add_argument("--cpus", type=int, default=4)
p.add_argument("--timeout", type=int, default=180)
p.add_argument("--spin", type=int, default=0)
p.add_argument("--kernel", type=pathlib.Path)
p.add_argument("--result-name", help="Distinct name for controlled comparisons")
a = p.parse_args()
if not 1 <= a.cpus <= 64 or (a.mode == "ptrace" and a.cpus != 1):
    p.error("ptrace requires one CPU; seccomp supports 1..64 configured CPUs")
if not 0 <= a.spin <= 10000:
    p.error("spin must be 0..10000")
work = a.work.resolve()
results = work / "results"
results.mkdir(parents=True, exist_ok=True)
tmp = work / "tmp"
tmp.mkdir(exist_ok=True)
name = f"{a.mode}-{a.cpus}cpu-spin{a.spin}"
if a.result_name:
    if not all(c.isalnum() or c in "-_" for c in a.result_name):
        p.error("result-name must contain only letters, digits, hyphens and underscores")
    name = a.result_name
log = results / (name + ".log")
kernel = a.kernel.resolve() if a.kernel else work / "kernel-build/linux"
cow = tmp / ("core-" + uuid.uuid4().hex + ".cow")
cmd = [str(kernel), "umid=harddind-" + uuid.uuid4().hex[:12], "mem=2G", f"ncpus={a.cpus}",
       "seccomp=" + ("on" if a.mode == "seccomp" else "off"),
       f"ubd0={cow},{work / 'probe.ext4'}", "root=/dev/ubda", "rw", "init=/sbin/harddind-init",
       "con=null", "con0=null,fd:1", "ssl=null", "panic=1"]
if a.spin:
    cmd.append(f"seccomp_spin={a.spin}")
started = time.monotonic()
usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
timed_out = False
with log.open("w") as output:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
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
usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
rows = []
for line in text.splitlines():
    if line.startswith('{"test":'):
        rows.append(json.loads(line))
expected = {(test, workers) for test, workers in
            [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1), ("processes", 4)]}
counts = {key: sum((r["test"], r["workers"]) == key for r in rows) for key in expected}
passed = not timed_out and rc == 0 and "HARDDIND_PROBE_END status=0" in text and len(rows) == 18 and all(n == 3 for n in counts.values())
report = {"kind": "kernel_microbench_only", "mode": a.mode, "cpus": a.cpus,
          "spin": a.spin,
          "kernel_sha256": hashlib.sha256(kernel.read_bytes()).hexdigest(),
          "benchmark_sha256": hashlib.sha256((work / "probe-root/corebench").read_bytes()).hexdigest(),
          "passed": passed, "exit_code": rc, "timed_out": timed_out,
          "wall_seconds": time.monotonic() - started, "measurements": rows,
          "host_cpu_seconds": usage_after.ru_utime + usage_after.ru_stime - usage_before.ru_utime - usage_before.ru_stime,
          "docker_acceptance": "not_tested", "gpu_acceptance": "not_tested"}
(results / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
if not passed:
    print("\n".join(text.splitlines()[-35:]), file=sys.stderr)
sys.exit(0 if passed else 1)
