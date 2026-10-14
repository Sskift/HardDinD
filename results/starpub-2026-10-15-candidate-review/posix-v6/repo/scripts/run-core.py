#!/usr/bin/env python3
"""Boot isolated UML tests; boot success alone is never a passing experiment."""
import argparse
import fcntl
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
from result_checks import memfd_backing_evidence, broker_cache_evidence, deferred_ipi_evidence, mprotect_evidence
from guest_cleanup import cleanup_group

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
p.add_argument("--mode", choices=["ptrace", "seccomp"], default="seccomp")
p.add_argument("--cpus", type=int, default=4)
p.add_argument("--timeout", type=int, default=180)
p.add_argument("--spin", type=int, default=0)
p.add_argument("--kernel", type=pathlib.Path)
p.add_argument("--memfd-ram", action="store_true")
p.add_argument("--memfd-stub", action="store_true")
p.add_argument("--threads-experiment", action="store_true")
p.add_argument("--broker-cache", action="store_true")
p.add_argument("--defer-ipi", action="store_true")
p.add_argument("--mprotect-sync", action="store_true")
p.add_argument("--mapping-semantics", action="store_true")
p.add_argument("--thread-semantics", action="store_true")
p.add_argument("--dispatch-bench", action="store_true")
p.add_argument("--disk", type=pathlib.Path)
p.add_argument("--result-name", help="Distinct name for controlled comparisons")
a = p.parse_args()
if a.mapping_semantics and not a.dispatch_bench:
    p.error("mapping fixture includes the dispatch suite; use --dispatch-bench")
if a.mprotect_sync and not a.threads_experiment:
    p.error("mprotect synchronization requires parallel runners")
if a.broker_cache and not a.threads_experiment:
    p.error("broker cache requires parallel runners")
if not 1 <= a.cpus <= 64 or (a.mode == "ptrace" and a.cpus != 1):
    p.error("ptrace requires one CPU; seccomp supports 1..64 configured CPUs")
if not 0 <= a.spin <= 10000:
    p.error("spin must be 0..10000")
work = a.work.resolve()
# Builders take an exclusive lock; nested runners may share this read lock.
# Keep the descriptor alive for the complete run, including native baselines.
build_lock = (work / "kernel-build.lock").open("a")
fcntl.flock(build_lock, fcntl.LOCK_SH)
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
if log.exists() or (results / (name + ".json")).exists():
    p.error("refusing to overwrite evidence")
kernel = a.kernel.resolve() if a.kernel else work / "kernel-build/linux"
disk = a.disk.resolve() if a.disk else work / "probe.ext4"
cow = tmp / ("core-" + uuid.uuid4().hex + ".cow")
cmd = [str(kernel), "umid=harddind-" + uuid.uuid4().hex[:12], "mem=2G", f"ncpus={a.cpus}",
       "seccomp=" + ("on" if a.mode == "seccomp" else "off"),
       f"ubd0={cow},{disk}", "root=/dev/ubda", "rw", "init=/sbin/harddind-init",
       "con=null", "con0=null,fd:1", "ssl=null", "panic=1"]
if a.thread_semantics:
    cmd[cmd.index("init=/sbin/harddind-init")] = "init=/sbin/harddind-thread-init"
if a.dispatch_bench:
    cmd = ["init=/sbin/harddind-dispatch-init" if arg.startswith("init=") else arg for arg in cmd]
if a.mapping_semantics:
    cmd = ["init=/sbin/harddind-mapping-init" if arg.startswith("init=") else arg for arg in cmd]
if a.threads_experiment:
    if a.mode != "seccomp":
        p.error("parallel runners require seccomp")
    cmd.append("seccomp_threads=1")
if a.spin:
    cmd.append(f"seccomp_spin={a.spin}")
if a.broker_cache:
    cmd.append("seccomp_broker_cache=1")
if a.defer_ipi:
    cmd.append("defer_ipi=1")
if a.mprotect_sync:
    cmd.append("mprotect_sync=1")
if a.memfd_ram: cmd.append("memfd_ram=1")
if a.memfd_stub: cmd.append("memfd_stub=1")
kernel_sha256 = hashlib.sha256(kernel.read_bytes()).hexdigest()
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
        cleanup = cleanup_group(proc.pid)
        cow.unlink(missing_ok=True)
text = log.read_text(errors="replace")
kernel_unchanged = hashlib.sha256(kernel.read_bytes()).hexdigest() == kernel_sha256
usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
rows = []
for line in text.splitlines():
    if line.startswith('{"test":'):
        rows.append(json.loads(line))
expected = {(test, workers) for test, workers in
            [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1), ("processes", 4)]}
dispatch_cases = {("identity", 1), ("fdio", 1), ("mapping", 1), ("thread_syscalls", 4), ("isolation", 1), ("parallel_exec", 4)}
if a.dispatch_bench:
    expected |= dispatch_cases
counts = {key: sum((r["test"], r["workers"]) == key for r in rows) for key in expected}
passed = kernel_unchanged and not timed_out and rc == 0 and "HARDDIND_PROBE_END status=0" in text and len(rows) == 3 * len(expected) and all(n == 3 for n in counts.values())
passed = passed and cleanup["clean_shutdown"]
semantics_expected = {"shared_memory_tls_futex_migration", "mprotect_munmap_remap", "concurrent_fork_cow", "thread_lifecycle", "concurrent_page_table_teardown"}
semantics_lines = [line.split() for line in text.splitlines() if line.startswith("THREAD_CHECK ")]
semantics_passed = (len(semantics_lines) == len(semantics_expected) and
                    {row[1] for row in semantics_lines if len(row) == 3 and row[2] == "pass"} == semantics_expected and
                    "THREAD_SEMANTICS_END status=0" in text)
dispatch_hash = next((line.split("=", 1)[1] for line in text.splitlines()
                      if line.startswith("HARDDIND_DISPATCH_SHA256=")), None)
if a.thread_semantics or a.dispatch_bench:
    passed = passed and semantics_passed
if a.dispatch_bench:
    expected_hash = hashlib.sha256((work / "dispatch-root/dispatchbench").read_bytes()).hexdigest()
    core_hash = hashlib.sha256((work / "probe-root/corebench").read_bytes()).hexdigest()
    passed = passed and f"HARDDIND_CORE_SHA256={core_hash}" in text.splitlines()
    passed = passed and dispatch_hash == expected_hash and all(
        r.get("verified") is True for r in rows if (r["test"], r["workers"]) in dispatch_cases)
cache_stats = broker_cache_evidence(text)
mapping_sha = None
mapping_passed = None
if a.mapping_semantics:
    mapping_sha = hashlib.sha256((work / "mapping-root/mapping-semantics").read_bytes()).hexdigest()
    mapping_expected = {"lazy_discard_and_protection", "fixed_file_replacement_private_cow", "fork_cow_and_file_truncate"}
    mapping_rows = [line.split() for line in text.splitlines() if line.startswith("MAPPING_CHECK ")]
    mapping_passed = (len(mapping_rows) == len(mapping_expected) and
                      {row[1] for row in mapping_rows if len(row) == 3 and row[2] == "pass"} == mapping_expected and
                      "MAPPING_SEMANTICS_END status=0" in text.splitlines() and
                      f"HARDDIND_MAPPING_SHA256={mapping_sha}" in text.splitlines())
    passed = passed and mapping_passed
ipi_stats = deferred_ipi_evidence(text)
protect_stats = mprotect_evidence(text)
backing = memfd_backing_evidence(text, a.memfd_ram, a.memfd_stub)
if a.memfd_ram or a.memfd_stub:
    passed = passed and backing["verified"]
if a.mprotect_sync:
    passed = passed and protect_stats["verified"]
if a.defer_ipi:
    passed = passed and ipi_stats["verified"]
if a.broker_cache:
    # A kernel that ignores the boot option must not count as cache evidence.
    passed = passed and cache_stats["verified"]
report = {"kind": "kernel_microbench_only", "mode": a.mode, "cpus": a.cpus,
          "spin": a.spin, "threads_experiment": a.threads_experiment,
          "broker_cache": a.broker_cache,
          "broker_cache_stats": cache_stats,
          "memfd_ram": a.memfd_ram, "memfd_stub": a.memfd_stub, "memory_backing": backing,
          "defer_ipi": a.defer_ipi, "deferred_ipi_stats": ipi_stats,
          "mprotect_sync": a.mprotect_sync, "mprotect_stats": protect_stats,
          "mapping_semantics": mapping_passed, "mapping_semantics_sha256": mapping_sha,
          "dispatch_benchmark_sha256": dispatch_hash,
          "thread_semantics": semantics_passed if a.thread_semantics or a.dispatch_bench else "not_tested",
          "thread_semantics_sha256": next((line.split("=", 1)[1] for line in text.splitlines()
                                           if line.startswith("HARDDIND_THREAD_SHA256=")), None),
          "host_cpu_affinity": sorted(os.sched_getaffinity(0)),
          "kernel_sha256": kernel_sha256, "kernel_unchanged": kernel_unchanged,
          "cleanup": cleanup,
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
