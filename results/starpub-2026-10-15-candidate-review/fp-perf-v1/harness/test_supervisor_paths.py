#!/usr/bin/env python3
"""Actual supervisor-path tests with tiny FAKE executables and real Popen.

No real benchmark, kernel or guest is used:
  * run_native() end to end against shell-script fakes that emit the exact
    frozen-source JSON schemas (real Popen, real taskset, real cleanup);
  * run_native per-run timeout + cell deadline;
  * launch() kills and reaps a stuck leader WITH a surviving group child;
  * finalize_cell() on valid and corrupt console logs;
  * lock acquisition fails within a bounded deadline;
  * summarize() excludes errored/budget cells from every ratio.

Run under the global EX lock with a hard timeout (owner allowance: tiny
fake processes only, <=180 s command).
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "fp_perf_supervisor", HERE / "fp-perf-supervisor.py")
mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # noqa: E402
from perf_log import WORKLOADS, ITERS  # noqa: E402

COREBENCH_FAKE = r'''#!/bin/sh
# fake corebench: exact frozen output schema; optional slow mode
wl=$1; w=$2
if [ "${PERF_FAKE_SLOW:-0}" = "1" ] && [ "$wl" = "syscall" ]; then
    trap '' TERM
    sleep 30
fi
case "$wl" in
  threads|processes) n=$w;;
  *) n=1;;
esac
printf '{"test":"%s","workers":%d,"seconds":0.001000000}\n' "$wl" "$n"
'''

DISPATCH_FAKE = r'''#!/bin/sh
# fake dispatchbench: exact frozen one-line schema incl "verified":true
[ "$1" = thread_syscalls ] || exit 2
printf '{"test":"thread_syscalls","workers":4,"seconds":0.002000000,"verified":true}\n'
'''


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


def make_workdir():
    d = pathlib.Path(tempfile.mkdtemp(prefix="perf-fake-"))
    (d / "bin").mkdir()
    (d / "tmp").mkdir()
    write_exec(d / "bin" / "corebench", COREBENCH_FAKE)
    write_exec(d / "bin" / "dispatchbench", DISPATCH_FAKE)
    return d


def make_supervisor(work, **kw):
    a = argparse.Namespace(
        work=work, budget=kw.get("budget", 1200),
        run_timeout=kw.get("run_timeout", 10), boot_deadline=30,
        native_deadline=kw.get("native_deadline", 60), no_prep=True,
        lock=pathlib.Path(tempfile.mktemp(prefix="perf-lock-")))
    sup = mod.Supervisor(a)
    sup.man = {
        "binaries": {"corebench": mod.sha(work / "bin" / "corebench"),
                     "dispatchbench": mod.sha(work / "bin" / "dispatchbench"),
                     "perfrun": "0" * 64},
        "harness": {}}
    return sup


def fake_man(work):
    return {"corebench": mod.sha(work / "bin" / "corebench"),
            "dispatchbench": mod.sha(work / "bin" / "dispatchbench")}


class SupervisorPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if subprocess.call(["which", "taskset"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            raise RuntimeError("taskset required for path tests")

    def setUp(self):
        self.work = make_workdir()
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    # 1. actual run_native happy path (text-mode emit fix included) -------
    def test_run_native_happy(self):
        sup = make_supervisor(self.work, native_deadline=60)
        cell = sup.run_native("native-pre", deadline=60)
        self.assertEqual(cell["errors"], [], cell["errors"])
        self.assertTrue(cell["clean_shutdown"])
        self.assertEqual(cell["exit_code"], 0)
        for binary, wl, _a, _n in WORKLOADS:
            key = "%s/%s" % (binary, wl)
            self.assertIn(key, cell["stats"], key)
            self.assertEqual(cell["stats"][key]["n"], 5, key)
        log = (self.work / "native-pre.stdout").read_text()
        self.assertIn("HARDDIND_PERF_BEGIN", log)
        self.assertIn("HARDDIND_PERF_END status=0", log)
        self.assertEqual(log.count("HARDDIND_PERF_RUN_RC=0"),
                         len(WORKLOADS) * ITERS)

    # 2. per-run timeout + hard cell deadline ------------------------------
    def test_run_native_timeouts_bounded(self):
        sup = make_supervisor(self.work, run_timeout=1,
                              native_deadline=4)
        old_env = os.environ.copy()
        os.environ["PERF_FAKE_SLOW"] = "1"
        try:
            t0 = time.monotonic()
            cell = sup.run_native("native-pre", deadline=4)
            elapsed = time.monotonic() - t0
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        # hard bound: 4 s cell deadline + reap margin, never +run_timeout
        self.assertLess(elapsed, 9.0,
                        "cell overran deadline: %.2f" % elapsed)
        self.assertTrue(cell["errors"])
        self.assertTrue(cell["timed_out"])
        self.assertTrue(any("timed out" in e for e in cell["errors"]))

    # 3. launch(): stuck group leader + surviving child are reaped ---------
    def test_launch_kills_whole_group(self):
        sup = make_supervisor(self.work)
        row = sup.launch(
            "stuck", ["/bin/sh", "-c", "sleep 30 & wait"], deadline=1)
        self.assertTrue(row["timed_out"])
        self.assertTrue(row["cleanup"]["clean_shutdown"], row["cleanup"])
        self.assertEqual(row["cleanup"]["remaining_live_pids"], [])
        self.assertEqual(row["cleanup"]["remaining_zombie_pids"], [])
        # whole group SIGKILL: leader wait returns signal death (-9/-15)
        self.assertIn(row["exit_code"], (-9, -15))

    # 4. finalize_cell on valid and corrupt logs ---------------------------
    def _valid_log(self):
        m = fake_man(self.work)
        lines = ["HARDDIND_PERF_BEGIN",
                 "HARDDIND_PERF_COREBENCH_SHA256=" + m["corebench"],
                 "HARDDIND_PERF_DISPATCHBENCH_SHA256="
                 + m["dispatchbench"]]
        for binary, wl, args, workers in WORKLOADS:
            for seq in range(1, ITERS + 1):
                warm = 1 if seq <= 2 else 0
                lines.append("HARDDIND_PERF_RUN binary=%s workload=%s "
                             "args=%s warmup=%d seq=%d"
                             % (binary, wl, args, warm, seq))
                extra = ',"verified":true' if binary == "dispatchbench" \
                    else ""
                lines.append('{"test":"%s","workers":%d,"seconds":0.005%s}'
                             % (wl, workers, extra))
                lines.append("HARDDIND_PERF_RUN_RC=0")
        lines.append("HARDDIND_PERF_END status=0")
        return "\n".join(lines) + "\n"

    def test_finalize_valid_and_corrupt(self):
        sup = make_supervisor(self.work)
        cases = (("g1", self._valid_log(), True),
                 ("g2", self._valid_log().replace('"workers":1',
                                                  '"workers":9', 1), False))
        for label, text, should_pass in cases:
            (self.work / (label + ".stdout")).write_text(text)
            (self.work / (label + ".stderr")).write_text("")
            cell = sup.finalize_cell(
                label, "guest", "lazy", True, exit_code=0,
                timed_out=False,
                cleanup={"clean_shutdown": True}, extra_errors=None)
            if should_pass:
                self.assertEqual(cell["errors"], [], cell["errors"])
                self.assertEqual(len(cell["stats"]), len(WORKLOADS))
            else:
                self.assertTrue(any("requires 1" in e
                                    for e in cell["errors"]),
                                cell["errors"])

    # 5. bounded lock acquisition ------------------------------------------
    def test_lock_deadline(self):
        lf = self.work / "held.lock"
        holder = subprocess.Popen(
            ["flock", str(lf), "sleep", "8"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        sup = make_supervisor(self.work, budget=1200)
        sup.a.lock = lf
        sup.t0 = time.monotonic() - 1199  # ~1 s of budget remains
        t0 = time.monotonic()
        with self.assertRaises(mod.LockDeadline):
            sup.acquire("SH")
        self.assertLess(time.monotonic() - t0, 3.0)

    # 6. summarize excludes errored/budget cells, guards missing stats ------
    def test_summary_exclusions(self):
        sup = make_supervisor(self.work)
        wls = ["%s/%s" % (b, w) for b, w, _a, _n in WORKLOADS]
        good = {"errors": [], "clean_shutdown": True, "timed_out": False,
                "stats": {k: {"n": 5, "median": 1.0, "mad": 0.0,
                              "min": 0.9, "raw_seconds": [1] * 5}
                          for k in wls}}
        labels = [c[0] for c in mod.CELLS]
        sup.status["cells"] = {l: json.loads(json.dumps(good))
                               for l in labels}
        out = sup.summarize()
        self.assertEqual(out["ratios"]["excluded_cells"], {})
        sup.status["cells"]["lazy1-b"]["errors"] = ["boom"]
        out = sup.summarize()
        self.assertIn("lazy1-b", out["ratios"]["excluded_cells"])
        wl = "corebench/syscall"
        self.assertNotIn("lazy1-b/eager-a",
                         out["ratios"]["lazy1_over_eager"][wl])
        self.assertIn("lazy1-a/eager-a",
                      out["ratios"]["lazy1_over_eager"][wl])
        sup.status["cells"]["eager-b"] = {
            "status": "not_run_budget",
            "errors": ["budget"], "clean_shutdown": True}
        out = sup.summarize()
        self.assertIn("eager-b", out["ratios"]["excluded_cells"])
        self.assertTrue(out["no_threshold_or_verdict"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
