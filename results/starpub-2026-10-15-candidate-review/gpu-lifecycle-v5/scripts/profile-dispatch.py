#!/usr/bin/env python3
"""Isolated phase measurements and host syscall traces; no acceptance claims."""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import resource
import signal
import subprocess
import time
import uuid
from guest_cleanup import cleanup_group
from result_checks import deferred_ipi_evidence, mprotect_evidence

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--kernel", type=pathlib.Path, required=True)
p.add_argument("--mode", choices=["native", "trap", "guest", "trace"], required=True)
p.add_argument("--spin", type=int, default=256)
p.add_argument("--defer-ipi", action="store_true")
p.add_argument("--mprotect-sync", action="store_true")
p.add_argument("--name", required=True)
p.add_argument("--timeout", type=int, default=240)
a = p.parse_args()
if not a.name or not all(c.isalnum() or c in "-_" for c in a.name):
    p.error("invalid name")
if not 0 <= a.spin <= 10000:
    p.error("invalid spin")
if a.defer_ipi and a.mode in ("native", "trap"):
    p.error("deferred IPIs apply only to guests")
if a.mprotect_sync and a.mode in ("native", "trap"):
    p.error("mprotect synchronization applies only to guests")
work = a.work.resolve()
lock = (work / "kernel-build.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_SH)
os.sched_setaffinity(0, [0, 1, 2, 3])
kernel = a.kernel.resolve()
binary = work / "profile-root/profilebench"
disk = work / "profile.ext4"
out = work / "results"
log = out / (a.name + ".log")
trace = out / (a.name + ".strace")
result = out / (a.name + ".json")
if any(path.exists() for path in (log, trace, result)):
    p.error("refusing to overwrite evidence")
tmp = work / "tmp"
cow = tmp / ("profile-" + uuid.uuid4().hex + ".cow")
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
hashes = {"kernel": sha(kernel), "benchmark": sha(binary), "disk": sha(disk)}
def host_state():
    return {name: pathlib.Path(path).read_text() for name, path in {
        "cpu_stat": "/sys/fs/cgroup/cpu.stat", "cpu_pressure": "/sys/fs/cgroup/cpu.pressure",
        "loadavg": "/proc/loadavg", "memory_events": "/sys/fs/cgroup/memory.events"}.items()}
if a.mode in ("native", "trap"):
    cmd = [str(binary), "time"] + (["trap"] if a.mode == "trap" else [])
else:
    cmd = [str(kernel), "umid=harddind-profile-" + uuid.uuid4().hex[:12],
           "mem=2G", "ncpus=4", "seccomp=on", "seccomp_threads=1", "seccomp_broker_cache=1",
           f"seccomp_spin={a.spin}", f"ubd0={cow},{disk}", "root=/dev/ubda", "rw",
           "init=/sbin/harddind-profile-init", "con=null", "con0=null,fd:1", "ssl=null", "panic=1"]
    if a.defer_ipi:
        cmd += ["defer_ipi=1"]
    if a.mprotect_sync:
        cmd += ["mprotect_sync=1"]
    if a.mode == "trace":
        cmd += ["profile_trace=1"]
        cmd = ["strace", "-f", "-qq", "-tt", "-s", "256", "-o", str(trace),
               "-e", "trace=futex,rt_sigprocmask,rt_sigreturn,mmap,munmap,mprotect,clone,clone3,execve,execveat,sendmsg,recvmsg,close,write,wait4,kill,arch_prctl,memfd_create",
               *cmd]
before = host_state()
usage0 = resource.getrusage(resource.RUSAGE_CHILDREN)
started = time.monotonic()
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
        cleanup = cleanup_group(proc.pid)
        cow.unlink(missing_ok=True)
elapsed = time.monotonic() - started
after = host_state()
usage1 = resource.getrusage(resource.RUSAGE_CHILDREN)
content = log.read_text(errors="replace")
rows = [json.loads(line[len("PROFILE_END "):]) for line in content.splitlines() if line.startswith("PROFILE_END ")]
expected_cases = {"getpid"} if a.mode == "trap" else {
    "empty", "getpid", "map_none", "map_1page", "map_16pages", "map_protect", "reuse_16pages", "protect_pair", "fork", "exec"}
repeats = 1 if a.mode == "trace" else 3
valid = len(rows) == len(expected_cases) * repeats and {(r["case"], r["repeat"]) for r in rows} == {
    (case, rep) for case in expected_cases for rep in range(repeats)} and all(r["verified"] for r in rows)
unchanged = hashes == {"kernel": sha(kernel), "benchmark": sha(binary), "disk": sha(disk)}
passed = rc == 0 and not timed_out and cleanup["clean_shutdown"] and valid and unchanged and "PROFILE_COMPLETE" in content
if a.mode in ("guest", "trace"):
    passed = passed and "PROFILE_STATUS=0" in content and f"PROFILE_SHA256={hashes['benchmark']}" in content
ipi_stats = deferred_ipi_evidence(content)
protect_stats = mprotect_evidence(content)
if a.mprotect_sync:
    passed = passed and protect_stats["verified"]
if a.defer_ipi:
    passed = passed and ipi_stats["verified"]
report = {"kind": "diagnostic_only", "mode": a.mode, "spin": a.spin, "sha256": hashes,
          "host_cpu_affinity": sorted(os.sched_getaffinity(0)), "command": cmd,
          "defer_ipi": a.defer_ipi, "deferred_ipi_stats": ipi_stats,
          "mprotect_sync": a.mprotect_sync, "mprotect_stats": protect_stats,
          "passed": passed, "exit_code": rc, "timed_out": timed_out, "cleanup": cleanup,
          "wall_seconds": elapsed, "host_before": before, "host_after": after,
          "host_cpu_seconds": usage1.ru_utime + usage1.ru_stime - usage0.ru_utime - usage0.ru_stime,
          "voluntary_context_switches": usage1.ru_nvcsw - usage0.ru_nvcsw,
          "involuntary_context_switches": usage1.ru_nivcsw - usage0.ru_nivcsw,
          "measurements": rows, "timings_usable": a.mode != "trace", "product_acceptance": False}
result.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({k: report[k] for k in ("mode", "passed", "exit_code", "wall_seconds", "host_cpu_seconds", "cleanup")}))
if not passed:
    print("\n".join(content.splitlines()[-30:]))
raise SystemExit(0 if passed else 1)
