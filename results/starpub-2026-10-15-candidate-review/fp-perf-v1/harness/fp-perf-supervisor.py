#!/usr/bin/env python3
"""Owner-run, bounded, DESCRIPTIVE performance supervisor (v2) for 0018.

Answers only the first focused question: does lazy XSTATE change actual
syscall/signal cost, and 4-worker parallel compute, on the frozen eager vs
lazy UML kernels? Medians/MADs/minima and ratios only - NO pass/fail
performance verdict, NO tolerance; native brackets and the lazy0 control
are never pooled with anything.

Cell order (fresh boot per guest cell; native brackets run the same static
binaries directly on the host):
  native-pre (EX), eager-a (SH), lazy1-a (SH), lazy1-b (SH), eager-b (SH),
  lazy0-control (SH, lazy binary WITHOUT flag), native-post (EX).

Bounds are real:
  * locks are acquired nonblocking with deadline = global budget elapsed;
  * every launch gets min(requested deadline, remaining global budget);
  * native cell: each invocation gets min(run_timeout, remaining cell
    deadline), so a cell cannot overrun by a per-run timeout;
  * all launched process groups are cleaned (even on interruption),
    streams always close, COW files always unlink.

Per-step state is fsynced to perf-events.jsonl BEFORE Popen and again once
the PID exists, with full cleanup identities at completion. Raw
stdout/stderr go to per-cell files opened exclusively in TEXT mode.

Usage:
  python3 fp-perf-supervisor.py [--work DIR] [--budget 1200]
      [--run-timeout 45] [--boot-deadline 180] [--native-deadline 120]
      [--no-prep] [--lock PATH]
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import statistics
import subprocess
import sys
import time
import traceback
import uuid

HERE = pathlib.Path(__file__).resolve().parent
MATRIX = pathlib.Path("/root/harddind-rebuild/fp-matrix-owner-v1")
SRC_DIR = pathlib.Path("/root/harddind-rebuild/fp-owner-review-v4/src")
REPO_SCRIPTS = MATRIX / "repo" / "scripts"
sys.path.insert(0, str(REPO_SCRIPTS))
from guest_cleanup import cleanup_group, group_members  # noqa: E402
from perf_log import WORKLOADS, ITERS, parse_perf_text, timed_records  # noqa

KERNEL_SHA = {
    "eager": "a0bc79d9be2d61b0dbdef8bb34a0fad65f98d8046f196bf21ae663e47c503055",
    "lazy": "9f957ded67f8ee74a6178372db7c7acda6aea5581d837603e786f8312a9f836e",
}
SOURCE_SHA = {
    "corebench.c":
        "991be1c676c293e5cdc8900e0ba23fab23fb3de2a65bb19702b5472069c23a35",
    "dispatchbench.c":
        "84d6dd6c0e154b8291bce9d69de4cac8e359fe70c5816a77b5c7d9a043c8eba7",
}
# Harness files whose drift invalidates the run (manifest pins hashes).
HARNESS_FILES = ("fp-perf-supervisor.py", "perf_log.py",
                 "harddind-perf-init", "perfrun.c", "test_perf_log.py")
CLEANUP_LIB = REPO_SCRIPTS / "guest_cleanup.py"
AFFINITY = ["taskset", "-c", "0-3"]
MEM = "2G"
CPUS = 4

# label: (kind, kernel-or-None, lazyxstate flag)
CELLS = [
    ("native-pre",    "native", None,  None),
    ("eager-a",       "guest",  "eager", False),
    ("lazy1-a",       "guest",  "lazy",  True),
    ("lazy1-b",       "guest",  "lazy",  True),
    ("eager-b",       "guest",  "eager", False),
    ("lazy0-control", "guest",  "lazy",  False),
    ("native-post",   "native", None,  None),
]
GUEST_LABELS = [c[0] for c in CELLS if c[1] == "guest"]


class LockDeadline(RuntimeError):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Supervisor:
    def __init__(self, args):
        self.a = args
        self.work = args.work.resolve()
        (self.work / "tmp").mkdir(parents=True, exist_ok=True)
        self.status_path = self.work / "perf-status.json"
        self.events_path = self.work / "perf-events.jsonl"
        self.audit_path = self.work / "perf-cleanup-audit.json"
        self.summary_json = self.work / "perf-summary.json"
        self.summary_md = self.work / "perf-summary.md"
        self.lock_file = None
        self.man = None
        self.status = {
            "status": "running", "stage": "init", "pid": os.getpid(),
            "started_at": now_iso(),
            "config": {"budget": args.budget, "run_timeout": args.run_timeout,
                       "boot_deadline": args.boot_deadline,
                       "native_deadline": args.native_deadline,
                       "affinity": "0-3", "mem": MEM, "ncpus": CPUS,
                       "workloads": [list(w[:3]) for w in WORKLOADS],
                       "expected_workers": {w[1]: w[3] for w in WORKLOADS},
                       "warmups": 2, "timed": 5},
            "frozen": {"kernels": KERNEL_SHA, "sources": SOURCE_SHA},
            "cells": {}, "events": 0, "inputs_unchanged": None,
        }
        self.t0 = time.monotonic()
        self.save()

    def remaining_budget(self):
        return max(0.0, self.a.budget - (time.monotonic() - self.t0))

    # ---- bookkeeping -----------------------------------------------------
    def save(self):
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.status, indent=2) + "\n")
        tmp.replace(self.status_path)

    def event(self, **row):
        row["ts"] = round(time.monotonic() - self.t0, 6)
        row["wall"] = now_iso()
        with self.events_path.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.status["events"] += 1

    def stage(self, name):
        self.status["stage"] = name
        self.event(kind="stage", name=name)
        self.save()

    # ---- locking (bounded, nonblocking within global budget) ------------
    def acquire(self, mode):
        name = fcntl.LOCK_EX if mode == "EX" else fcntl.LOCK_SH
        while True:
            remain = self.remaining_budget()
            if remain <= 0:
                raise LockDeadline(
                    "global budget exhausted waiting for %s lock" % mode)
            lf = self.a.lock.open("a")
            try:
                fcntl.flock(lf, name | fcntl.LOCK_NB)
            except BlockingIOError:
                lf.close()
                time.sleep(min(1.0, remain))
                continue
            self.lock_file = lf
            self.event(kind="lock_acquired", mode=mode,
                       lock=str(self.a.lock))
            return

    def release(self):
        if self.lock_file is not None:
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_UN)
            finally:
                self.lock_file.close()
            self.lock_file = None
            self.event(kind="lock_released")

    # ---- frozen input checks --------------------------------------------
    def verify_inputs(self):
        errs = []
        for fname, want in SOURCE_SHA.items():
            p = SRC_DIR / fname
            if not p.exists() or sha(p) != want:
                errs.append("source drift: %s" % fname)
        for kname, want in KERNEL_SHA.items():
            p = MATRIX / ("%s-kernel" % kname)
            if not p.exists() or sha(p) != want:
                errs.append("kernel drift/missing: %s" % kname)
        man_path = self.work / "perf-manifest.json"
        if not man_path.exists():
            return errs, None
        man = json.loads(man_path.read_text())
        for name, want in man.get("binaries", {}).items():
            p = self.work / "bin" / name
            if not p.exists() or sha(p) != want:
                errs.append("binary drift/missing: %s" % name)
        if not (self.work / "perf.ext4").exists() or \
                sha(self.work / "perf.ext4") != man.get("disk"):
            errs.append("perf disk drift/missing")
        for name, want in man.get("harness", {}).items():
            p = HERE / name if not name.startswith("repo:") else \
                REPO_SCRIPTS / name.split(":", 1)[1]
            if not p.exists() or sha(p) != want:
                errs.append("harness drift/missing: %s" % name)
        return errs, man

    # ---- process launch with strict cleanup -----------------------------
    def launch(self, label, cmd, deadline, env=None):
        """New session, bounded wait, whole-group cleanup; streams closed."""
        out_p = self.work / ("%s.stdout" % label)
        err_p = self.work / ("%s.stderr" % label)
        of = out_p.open("x")  # text mode; children write bytes transparently
        ef = err_p.open("x")
        timed_out = False
        code = None
        row = {"label": label, "command": cmd, "stdout": str(out_p),
               "stderr": str(err_p),
               "started_ts": round(time.monotonic() - self.t0, 6)}
        self.event(kind="launch", label=label, command=cmd,
                   deadline_s=round(deadline, 3))
        p = None
        cleanup = None
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=of,
                                 stderr=ef, env=env, start_new_session=True)
            row["pid"] = p.pid
            self.event(kind="pid", label=label, pid=p.pid)
            self.save()
            try:
                code = p.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.event(kind="timeout", label=label, pid=p.pid)
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    code = p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    code = -1
        finally:
            of.flush(); ef.flush(); of.close(); ef.close()
            if p is not None:
                cleanup = cleanup_group(p.pid)
                self.event(kind="cleanup", label=label,
                           clean_shutdown=cleanup["clean_shutdown"],
                           initial_live_pids=cleanup["initial_live_pids"],
                           remaining_live_pids=cleanup["remaining_live_pids"],
                           remaining_zombie_pids=
                           cleanup["remaining_zombie_pids"])
        row.update(exit_code=code, timed_out=timed_out,
                   cleanup=cleanup or {},
                   ended_ts=round(time.monotonic() - self.t0, 6))
        self.event(kind="done", label=label,
                   exit_code=code, timed_out=timed_out,
                   clean_shutdown=(cleanup or {}).get("clean_shutdown"))
        self.save()
        return row

    def launch_one(self, label, argv, timeout):
        """One bounded native invocation; cleanup runs even on interrupt."""
        self.event(kind="launch", label=label, command=argv,
                   deadline_s=round(timeout, 3))
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=self.out_f, stderr=self.err_f,
                             start_new_session=True)
        self.event(kind="pid", label=label, pid=p.pid)
        rc = None
        timed_out = False
        cleanup = None
        try:
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.event(kind="timeout", label=label, pid=p.pid)
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                rc = p.wait(timeout=15)
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=15)
            raise
        finally:
            cleanup = cleanup_group(p.pid)
            self.event(kind="cleanup", label=label,
                       clean_shutdown=cleanup["clean_shutdown"],
                       initial_live_pids=cleanup["initial_live_pids"],
                       remaining_live_pids=cleanup["remaining_live_pids"],
                       remaining_zombie_pids=cleanup["remaining_zombie_pids"])
        self.event(kind="done", label=label, exit_code=rc,
                   timed_out=timed_out,
                   clean_shutdown=cleanup["clean_shutdown"])
        return rc, timed_out, cleanup

    # ---- native bracket: synthesize the same protocol -------------------
    def run_native(self, label, deadline):
        out_f = (self.work / ("%s.stdout" % label)).open("x")  # TEXT mode
        err_f = (self.work / ("%s.stderr" % label)).open("x")
        self.out_f, self.err_f = out_f, err_f
        failures = []
        cell_end = time.monotonic() + min(deadline, self.remaining_budget())

        def emit(s):
            out_f.write(s + "\n")
            out_f.flush()

        try:
            emit("HARDDIND_PERF_BEGIN")
            emit("HARDDIND_PERF_COREBENCH_SHA256="
                 + self.man["binaries"]["corebench"])
            emit("HARDDIND_PERF_DISPATCHBENCH_SHA256="
                 + self.man["binaries"]["dispatchbench"])
            for binary, workload, args, _n in WORKLOADS:
                argv = AFFINITY + [str(self.work / "bin" / binary), workload]
                if args != "-":
                    argv.append(args)
                for seq in range(1, ITERS + 1):
                    warm = 1 if seq <= 2 else 0
                    emit("HARDDIND_PERF_RUN binary=%s workload=%s args=%s "
                         "warmup=%d seq=%d"
                         % (binary, workload, args, warm, seq))
                    remain = cell_end - time.monotonic()
                    if remain <= 0:
                        failures.append("cell deadline reached")
                        emit("HARDDIND_PERF_RUN_RC=124")
                        break
                    rc, run_timed, clean = self.launch_one(
                        "%s-%s-%d" % (label, workload, seq), argv,
                        timeout=min(self.a.run_timeout, remain))
                    emit("HARDDIND_PERF_RUN_RC=%d" % rc)
                    if run_timed:
                        failures.append("%s seq %d timed out"
                                        % (workload, seq))
                    if not clean["clean_shutdown"]:
                        failures.append("%s seq %d residual group"
                                        % (workload, seq))
                if any("deadline" in f for f in failures):
                    break
            emit("HARDDIND_PERF_END status=%d" % (1 if failures else 0))
        finally:
            out_f.flush(); out_f.close(); err_f.flush(); err_f.close()

        cell_deadline_hit = any("deadline" in f for f in failures)
        return self.finalize_cell(
            label, "native", None, None,
            exit_code=1 if failures else 0,
            timed_out=cell_deadline_hit,
            cleanup={"clean_shutdown":
                     not any("residual" in f for f in failures)},
            extra_errors=failures)

    # ---- guest boot ------------------------------------------------------
    def run_guest(self, label, kernel_name, lazy_flag):
        cow = self.work / "tmp" / ("perf-%s.cow" % uuid.uuid4().hex)
        kernel = MATRIX / ("%s-kernel" % kernel_name)
        cmd = [str(kernel), "umid=harddind-perf-" + uuid.uuid4().hex[:12],
               "mem=%s" % MEM, "ncpus=%d" % CPUS, "seccomp=on",
               "ubd0=%s,%s" % (cow, self.work / "perf.ext4"),
               "root=/dev/ubda", "rw",
               "init=/sbin/harddind-perf-init",
               "con=null", "con0=null,fd:1", "ssl=null", "panic=1",
               "seccomp_threads=1"]
        if lazy_flag:
            cmd.append("lazyxstate=1")
        env = {**os.environ, "TMPDIR": str(self.work / "tmp")}
        try:
            row = self.launch(
                label, cmd,
                min(self.a.boot_deadline, self.remaining_budget()), env=env)
            return self.finalize_cell(
                label, "guest", kernel_name, lazy_flag,
                exit_code=row["exit_code"], timed_out=row["timed_out"],
                cleanup=row["cleanup"])
        finally:
            existed = cow.exists()
            cow.unlink(missing_ok=True)
            self.event(kind="cow_cleanup", label=label, existed=existed,
                       remains=cow.exists())
            if cow.exists():
                self.status.setdefault("cow_leftovers", []).append(str(cow))

    def finalize_cell(self, label, kind, kernel_name, lazy_flag,
                      exit_code, timed_out, cleanup, extra_errors=None):
        text = (self.work / ("%s.stdout" % label)).read_text(errors="replace")
        text += "\n" + (self.work / ("%s.stderr" % label)).read_text(
            errors="replace")
        expect = {"corebench": self.man["binaries"]["corebench"],
                  "dispatchbench": self.man["binaries"]["dispatchbench"]}
        runs, errors = parse_perf_text(text, expect)
        errors = list(errors)
        if extra_errors:
            errors.extend(extra_errors)
        if exit_code != 0:
            errors.append("process exit_code=%s" % exit_code)
        if timed_out:
            errors.append("cell timed out")
        if not cleanup.get("clean_shutdown"):
            errors.append("process group cleanup not clean")
        stats = {}
        for binary, workload, _a, _n in WORKLOADS:
            vals = [r["json"]["seconds"]
                    for r in timed_records(runs)
                    if r["binary"] == binary and r["workload"] == workload]
            if vals:
                med = statistics.median(vals)
                stats["%s/%s" % (binary, workload)] = {
                    "n": len(vals), "median": med,
                    "mad": statistics.median(abs(v - med) for v in vals),
                    "min": min(vals), "raw_seconds": vals}
        cell = {"kind": kind, "kernel": kernel_name,
                "lazyxstate": lazy_flag, "exit_code": exit_code,
                "timed_out": timed_out, "errors": errors,
                "clean_shutdown": cleanup.get("clean_shutdown"),
                "stats": stats}
        self.status["cells"][label] = cell
        self.save()
        return cell

    # ---- summary: valid cells only, no thresholds ------------------------
    @staticmethod
    def cell_ok(cell):
        return bool(cell) and cell.get("errors") == [] and \
            cell.get("clean_shutdown") and not cell.get("timed_out") and \
            isinstance(cell.get("stats"), dict) and bool(cell["stats"])

    def summarize(self):
        cells = self.status["cells"]
        labels = [c[0] for c in CELLS]
        workloads = ["%s/%s" % (b, w) for b, w, _a, _n in WORKLOADS]
        excluded = {l: cells[l].get("errors", ["cell absent"])
                    for l in labels if not self.cell_ok(cells.get(l))}

        def med(label, wl):
            c = cells.get(label)
            if not self.cell_ok(c):
                return None
            return c["stats"].get(wl, {}).get("median")

        ratios = {"lazy1_over_eager": {}, "lazy0control_over_eager": {},
                  "guest_over_native_pre": {},
                  "guest_over_native_post": {},
                  "excluded_cells": excluded}
        for wl in workloads:
            ratios["lazy1_over_eager"][wl] = {}
            for lz in ("lazy1-a", "lazy1-b"):
                for ez in ("eager-a", "eager-b"):
                    a, b = med(lz, wl), med(ez, wl)
                    if a is not None and b:
                        ratios["lazy1_over_eager"][wl]["%s/%s" % (lz, ez)] \
                            = round(a / b, 6)
            l0 = med("lazy0-control", wl)
            ratios["lazy0control_over_eager"][wl] = {}
            for ez in ("eager-a", "eager-b"):
                ev = med(ez, wl)
                if l0 is not None and ev:
                    ratios["lazy0control_over_eager"][wl][
                        "lazy0-control/%s" % ez] = round(l0 / ev, 6)
            ratios["guest_over_native_pre"][wl] = {}
            ratios["guest_over_native_post"][wl] = {}
            for gl in GUEST_LABELS:
                gv = med(gl, wl)
                for nlabel, bucket in (("native-pre",
                                        "guest_over_native_pre"),
                                       ("native-post",
                                        "guest_over_native_post")):
                    nv = med(nlabel, wl)
                    if gv is not None and nv:
                        ratios[bucket][wl][gl] = round(gv / nv, 6)

        summary = {"descriptive_only": True,
                   "no_threshold_or_verdict": True,
                   "cell_order": labels,
                   "warmups_excluded": True,
                   "controls_not_pooled": ["native-pre", "native-post",
                                           "lazy0-control"],
                   "ratios": ratios}
        self.summary_json.write_text(json.dumps(summary, indent=2) + "\n")

        lines = ["# perf summary (DESCRIPTIVE: no threshold, no verdict)",
                 "",
                 "Median seconds of 5 timed repetitions; warmups excluded. "
                 "Native brackets and lazy0 control are never pooled. Cells "
                 "with parser/runtime/cleanup errors are EXCLUDED from "
                 "ratios (see perf-summary.json excluded_cells).", "",
                 "| cell | " + " | ".join(workloads) + " |",
                 "|" + "---|" * (len(workloads) + 1)]
        for label in labels:
            vals = []
            for wl in workloads:
                m = med(label, wl)
                vals.append("%.6f" % m if m is not None else "EXCLUDED/-")
            lines.append("| %s | %s |" % (label, " | ".join(vals)))
        lines += ["", "## Ratios (median/median)", "",
                  "Full lazy1 x eager cross-products, each guest cell vs "
                  "EACH native bracket separately, and the unpooled "
                  "lazy0 control; excluded cells listed: "
                  + str(self.summary_json.name), ""]
        self.summary_md.write_text("\n".join(lines) + "\n")
        return summary

    # ---- driver ----------------------------------------------------------
    def cell_lockdown(self, label, kind, kernel_name, lazy_flag):
        """Run one cell under the right lock; record lock failure as fail."""
        mode = "EX" if kind == "native" else "SH"
        try:
            self.acquire(mode)
        except LockDeadline as e:
            self.status["cells"][label] = {
                "kind": kind, "kernel": kernel_name, "lazyxstate": lazy_flag,
                "status": "not_run_lock_deadline",
                "errors": [str(e) + " (recorded failure, not a skip)"],
                "clean_shutdown": True, "stats": {}}
            self.event(kind="lock_deadline", label=label)
            self.save()
            return
        try:
            if kind == "native":
                self.run_native(label, self.a.native_deadline)
            else:
                self.run_guest(label, kernel_name, lazy_flag)
        finally:
            self.release()

    def run(self):
        exitcode = 1
        try:
            self.stage("verify_inputs")
            errs, man = self.verify_inputs()
            if errs and man is None and not self.a.no_prep:
                self.acquire("EX")
                try:
                    self.stage("prep")
                    r = self.launch(
                        "prep",
                        ["bash", str(HERE / "fp-perf-prep.sh"),
                         str(self.work), str(self.a.run_timeout)], 300)
                    if r["exit_code"] != 0 or \
                            not r["cleanup"]["clean_shutdown"]:
                        raise RuntimeError("prep failed: %s" % r)
                finally:
                    self.release()
                errs, man = self.verify_inputs()
            if errs:
                raise RuntimeError("input verification failed: %s" % errs)
            self.man = man
            self.status["manifest"] = man
            self.save()

            for label, kind, kernel_name, lazy_flag in CELLS:
                if self.remaining_budget() <= 0:
                    self.status["cells"][label] = {
                        "kind": kind, "kernel": kernel_name,
                        "lazyxstate": lazy_flag,
                        "status": "not_run_budget",
                        "errors": ["global budget exhausted; recorded "
                                   "failure, not a skip"],
                        "clean_shutdown": True, "stats": {}}
                    self.event(kind="budget_exhausted", label=label)
                    self.save()
                    continue
                self.stage(label)
                self.cell_lockdown(label, kind, kernel_name, lazy_flag)

            self.stage("final_verify")
            errs2, _ = self.verify_inputs()
            self.status["input_errors_after"] = errs2
            self.status["inputs_unchanged"] = not errs2
            self.summarize()
            ok_cells = all(self.cell_ok(c)
                           for c in self.status["cells"].values())
            exitcode = 0 if (
                self.status["inputs_unchanged"] and ok_cells and
                len(self.status["cells"]) == len(CELLS) and
                not self.status.get("cow_leftovers")) else 1
        except BaseException:
            self.status["error"] = traceback.format_exc()
        finally:
            self.release()
            groups = {}
            for line in self.events_path.read_text().splitlines():
                ev = json.loads(line)
                if ev.get("kind") == "pid":
                    groups[str(ev["pid"])] = group_members(ev["pid"])
            self.audit_path.write_text(json.dumps(groups, indent=2) + "\n")
            self.status["groups_empty"] = all(
                v == ([], []) for v in groups.values())
            if not self.status["groups_empty"]:
                exitcode = 1
            self.status.update(
                status="finished", exit_code=exitcode,
                finished_at=now_iso())
            self.save()
            print(json.dumps({"exit_code": exitcode,
                              "status_file": str(self.status_path),
                              "summary": str(self.summary_md),
                              "events": self.status["events"]}, indent=2))
            sys.exit(exitcode)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--work", type=pathlib.Path, default=HERE / "work")
    p.add_argument("--budget", type=int, default=1200)
    p.add_argument("--run-timeout", dest="run_timeout", type=int, default=45)
    p.add_argument("--boot-deadline", dest="boot_deadline", type=int,
                   default=180)
    p.add_argument("--native-deadline", dest="native_deadline", type=int,
                   default=120)
    p.add_argument("--no-prep", action="store_true")
    p.add_argument("--lock", type=pathlib.Path,
                   default=pathlib.Path("/root/harddind-rebuild/"
                                        "kernel-build.lock"))
    a = p.parse_args()
    Supervisor(a).run()


if __name__ == "__main__":
    main()
