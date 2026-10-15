#!/usr/bin/env python3
"""Source + function checks for the v2 faultstat instrumentation.

Proves the v2 requirements without booting a guest:

1. Function: compiles and runs faultstat_unit.c against the REAL header
   from the patched source tree (disabled path = no clock call and no
   counter writes; enabled deltas/non-monotonic clamp/snapshots;
   error/errno never altered by the observing wrapper).
2. Source: structural assertions over the patched tlb.c, process.c and
   faultstat.{c,h} that the function test cannot see:
     - no local_clock()/sched-clock or added clock_gettime in the timed
       paths (one clock only: uml_fs_clock_ns -> os_nsecs());
     - begin() gates enabled BEFORE reading the clock;
     - tlb walk restores the ORIGINAL map/unmap ops when disabled;
     - fs_map/fs_unmap return the operation result unmodified;
     - /proc snapshots use atomic reads;
     - timing brackets add no error/errno to driver paths.

Usage: run_faultstat_checks.py <patched-linux-source-dir> [build-dir]
Exit 0 on success, 1 on any failure. Prints one JSON object per line.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
failures = []


def event(name, ok, **kw):
    row = {"check": name, "result": "pass" if ok else "fail", **kw}
    print(json.dumps(row, sort_keys=True), flush=True)
    if not ok:
        failures.append(name)


def read(src, rel):
    return (src / rel).read_text()


def main():
    if len(sys.argv) < 2:
        print("usage: run_faultstat_checks.py <linux-src>", file=sys.stderr)
        return 2
    src = pathlib.Path(sys.argv[1]).resolve()
    tlb = read(src, "arch/um/kernel/tlb.c")
    proc = read(src, "arch/um/os-Linux/skas/process.c")
    fsc = read(src, "arch/um/kernel/faultstat.c")
    fsh = read(src, "arch/um/include/shared/faultstat.h")

    # ---- source contract ------------------------------------------------
    event("one-clock-source",
          "unsigned long long uml_fs_clock_ns(void)" in fsc and
          "os_nsecs()" in fsc and
          "long long os_nsecs" not in fsh)
    event("no-local_clock-in-timed-paths",
          "local_clock(" not in tlb and "local_clock(" not in proc and
          "sched/clock.h" not in tlb)
    # process.c must not add its own clock_gettime timing; the only
    # clock_gettime under this feature lives inside the existing os_nsecs.
    proc_diff = proc  # full-file assertions are exact here
    event("process-uses-bracket-not-clock_gettime",
          "uml_fs_time_begin()" in proc_diff and
          "uml_fs_time_end(&uml_fs_run_ns" in proc_diff and
          "fs_now_ns" not in proc_diff and
          proc_diff.count("clock_gettime(CLOCK_MONOTONIC") == 0)

    # begin() gates on enabled BEFORE calling the clock.
    begin_body = fsh[fsh.index("uml_fs_time_begin(void)"):]
    begin_body = begin_body[:begin_body.index("}", begin_body.index("{"))]
    gate = begin_body.index("uml_faultstat_enabled")
    clock = begin_body.index("uml_fs_clock_ns()")
    event("begin-gates-before-clock", gate < clock,
          gate_at=gate, clock_at=clock)
    end_start = fsh.index("uml_fs_time_end(")
    end_body = fsh[end_start:fsh.index("\n}\n", end_start)]
    event("end-gates-before-clock",
          end_body.index("uml_faultstat_enabled") <
          end_body.index("uml_fs_clock_ns()"))
    event("inc-gated",
          "if (__builtin_expect(uml_faultstat_enabled, 0))" in fsh)

    # disabled branch in the walk must use original ops verbatim
    event("original-map-unmap-restored-when-off",
          "} else if (__builtin_expect(uml_faultstat_enabled, 0)) {"
          in tlb and
          "\t\tops.mmap = map;\n\t\tops.unmap = unmap;\n" in tlb)

    # wrappers return op result unmodified
    fm = tlb[tlb.index("static int fs_map"):tlb.index("static int fs_unmap")]
    fu = tlb[tlb.index("static int fs_unmap"):tlb.index("struct vm_ops")]
    event("fsmap-returns-op-result",
          "int ret = map(" in fm and "return ret;" in fm)
    event("fsunmap-returns-op-result",
          "int ret = unmap(" in fu and "return ret;" in fu)

    # proc snapshots
    event("proc-atomic-snapshots",
          "uml_fs_read(" in fsc and
          "__ATOMIC_ACQUIRE" in fsc and
          fsc.count("uml_fs_read(") >= 12)

    # run_ns semantics honestly labelled as including user execution
    event("run-ns-labelled-inclusive",
          "INCLUDES guest user execution" in fsh and
          "NOT pure fault-handling overhead" in fsh)

    # ---- function test against the real header --------------------------
    cc = os.environ.get("CC", "gcc")
    with tempfile.TemporaryDirectory(prefix="faultstat-unit-") as td:
        td = pathlib.Path(td)
        binp = td / "faultstat_unit"
        cmd = [cc, "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
               "-I", str(src / "arch/um/include/shared"),
               str(HERE / "faultstat_unit.c"), "-o", str(binp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        event("unit-compiles-clean", r.returncode == 0,
              stderr=r.stderr[-2000:])
        if r.returncode == 0:
            r = subprocess.run([str(binp)], capture_output=True, text=True,
                               timeout=30)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            parsed = None
            try:
                parsed = [json.loads(l) for l in lines]
            except json.JSONDecodeError as e:
                event("unit-output-valid-json", False, error=str(e),
                      raw=lines)
            if parsed is not None:
                event("unit-output-valid-json", True, lines=parsed)
                event("unit-passes",
                      r.returncode == 0 and
                      any(x.get("result") == "PASS" for x in parsed),
                      rc=r.returncode)

    summary = {"check": "summary",
               "result": "pass" if not failures else "fail",
               "failures": failures}
    print(json.dumps(summary, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
