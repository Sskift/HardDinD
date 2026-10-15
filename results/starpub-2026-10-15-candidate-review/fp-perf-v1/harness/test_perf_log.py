#!/usr/bin/env python3
"""Pure parser fixtures for the HARDDIND_PERF protocol v2 (no workloads).

Run: python3 test_perf_log.py
"""
import unittest

from perf_log import WORKLOADS, ITERS, parse_perf_text, timed_records

SHA = {"corebench": "c" * 64, "dispatchbench": "d" * 64}
EXPECTED_WORKERS = {"syscall": 1, "signals": 1, "threads": 4,
                   "processes": 4, "thread_syscalls": 4}


def json_for(binary, wl, seq, sec=None, workers=None, verified=...):
    if workers is None:
        workers = EXPECTED_WORKERS[wl]
    if sec is None:
        sec = 0.001 * seq
    extras = ""
    if binary == "dispatchbench":
        v = "true" if verified is ... else ("true" if verified else "false")
        if verified is not ... or True:
            extras = ',"verified":%s' % v
    if verified is False and binary == "dispatchbench":
        extras = ',"verified":false'
    if verified is ... and binary != "dispatchbench":
        extras = ""
    return '{"test":"%s","workers":%d,"seconds":%.9f%s}' % (
        wl, workers, sec, extras)


def synth(mutate=None, drop=None, end="HARDDIND_PERF_END status=0",
          begin=True, timeout_at=None, raw_json=None, extra_rc=False,
          no_json_rc=None, sha_phase=None, extra_end=False,
          stray_json_after_end=False, duplicate_begin=False):
    """Build a valid log; mutators inject exactly one protocol violation.

    mutate(wl, seq) -> replacement JSON string for that run (or None to
    keep the valid default). raw_json: dict (wl,seq)->raw string.
    sha_phase: 'after_first_run' to misplace a SHA marker.
    """
    out = []

    def emit_begin():
        out.append("HARDDIND_PERF_BEGIN")
        if duplicate_begin:
            out.append("HARDDIND_PERF_BEGIN")

    if begin:
        emit_begin()
    sha_lines = [
        "HARDDIND_PERF_COREBENCH_SHA256=" + SHA["corebench"],
        "HARDDIND_PERF_DISPATCHBENCH_SHA256=" + SHA["dispatchbench"]]
    timeout_at = timeout_at or set()
    raw_json = raw_json or {}
    no_json_rc = no_json_rc or set()
    emitted_sha = [False]

    def emit_sha():
        if not emitted_sha[0]:
            out.extend(sha_lines)
            emitted_sha[0] = True

    for (binary, wl, args) in [(x[0], x[1], x[2]) for x in WORKLOADS]:
        for seq in range(1, ITERS + 1):
            warm = 1 if seq <= 2 else 0
            if drop and drop(wl, seq):
                continue
            if sha_phase == "after_first_run" and not emitted_sha[0] \
                    and out and out[-1].startswith("HARDDIND_PERF_RUN_RC"):
                pass
            if sha_phase != "after_first_run":
                emit_sha()
            out.append("HARDDIND_PERF_RUN binary=%s workload=%s args=%s "
                       "warmup=%d seq=%d" % (binary, wl, args, warm, seq))
            if sha_phase == "after_first_run" and not emitted_sha[0]:
                out.extend(sha_lines)
                emitted_sha[0] = True
            if (wl, seq) not in no_json_rc:
                if (wl, seq) in raw_json:
                    out.append(raw_json[(wl, seq)])
                elif mutate:
                    repl = mutate(binary, wl, seq)
                    out.append(repl if repl is not None
                               else json_for(binary, wl, seq))
                else:
                    out.append(json_for(binary, wl, seq))
            rc = 124 if (wl, seq) in timeout_at else 0
            out.append("HARDDIND_PERF_RUN_RC=%d" % rc)
            if extra_rc and (wl, seq) == ("syscall", 3):
                out.append("HARDDIND_PERF_RUN_RC=0")
    if sha_phase != "after_first_run":
        emit_sha()
    out.append(end)
    if extra_end:
        out.append(end)
    if stray_json_after_end:
        out.append('{"test":"syscall","workers":1,"seconds":1.0}')
    return "\n".join(out) + "\n"


class PerfLogTests(unittest.TestCase):
    def test_valid(self):
        runs, errors = parse_perf_text(synth(), SHA)
        self.assertEqual(errors, [])
        self.assertEqual(len(runs), 35)
        self.assertEqual(len(timed_records(runs)), 25)

    def test_hash_mismatch(self):
        _, errors = parse_perf_text(synth(), dict(SHA, corebench="a" * 64))
        self.assertTrue(any("corebench hash mismatch" in e for e in errors))

    def test_missing_sha_marker(self):
        text = synth().replace(
            "HARDDIND_PERF_COREBENCH_SHA256=" + SHA["corebench"] + "\n", "")
        _, errors = parse_perf_text(text, SHA)
        self.assertTrue(any("missing SHA marker corebench" in e
                            for e in errors))

    def test_sha_misplaced(self):
        _, errors = parse_perf_text(
            synth(sha_phase="after_first_run"), SHA)
        self.assertTrue(any("SHA marker corebench misplaced" in e
                            for e in errors))

    def test_timeout_rc124(self):
        runs, errors = parse_perf_text(
            synth(timeout_at={("syscall", 4)}), SHA)
        self.assertTrue(any("seq=4 rc=124" in e for e in errors), errors)
        self.assertEqual(len(timed_records(runs)), 24)

    def test_missing_run(self):
        _, errors = parse_perf_text(
            synth(drop=lambda wl, sq: (wl, sq) == ("syscall", 4)), SHA)
        self.assertTrue(any("sequence/count mismatch" in e for e in errors))

    def test_bad_json(self):
        _, errors = parse_perf_text(
            synth(raw_json={("syscall", 4): "{broken"}), SHA)
        self.assertTrue(any("malformed JSON" in e for e in errors))

    def test_json_outside_run(self):
        _, errors = parse_perf_text(
            synth(stray_json_after_end=True), SHA)
        self.assertTrue(any("JSON record outside a RUN" in e
                            for e in errors))

    # ---- v2 numeric/schema strictness ------------------------------------
    def test_bool_seconds_rejected(self):
        _, errors = parse_perf_text(synth(
            raw_json={("syscall", 4):
                      '{"test":"syscall","workers":1,"seconds":true}'}),
            SHA)
        self.assertTrue(any("seconds must be a number" in e for e in errors))

    def test_bool_workers_rejected(self):
        _, errors = parse_perf_text(synth(
            raw_json={("threads", 4):
                      '{"test":"threads","workers":true,"seconds":1.0}'}),
            SHA)
        self.assertTrue(any("workers must be a positive integer" in e
                            for e in errors))

    def test_zero_and_negative_seconds(self):
        for sec in ("0.0", "-1.0"):
            _, errors = parse_perf_text(synth(
                raw_json={("syscall", 4):
                          '{"test":"syscall","workers":1,"seconds":%s}'
                          % sec}), SHA)
            self.assertTrue(any("strictly positive" in e for e in errors),
                            sec)

    def test_nonfinite_seconds(self):
        for sec in ("1e999", "-1e999"):
            _, errors = parse_perf_text(synth(
                raw_json={("syscall", 4):
                          '{"test":"syscall","workers":1,"seconds":%s}'
                          % sec}), SHA)
            self.assertTrue(any("finite and strictly positive" in e
                                for e in errors), sec)

    def test_wrong_workers_threads(self):
        _, errors = parse_perf_text(synth(
            raw_json={("threads", 4):
                      '{"test":"threads","workers":3,"seconds":1.0}'}),
            SHA)
        self.assertTrue(any("requires 4" in e for e in errors))

    def test_wrong_workers_syscall(self):
        _, errors = parse_perf_text(synth(
            raw_json={("syscall", 4):
                      '{"test":"syscall","workers":4,"seconds":1.0}'}),
            SHA)
        self.assertTrue(any("requires 1" in e for e in errors))

    def test_dispatch_verified_required_true(self):
        missing = synth(raw_json={
            ("thread_syscalls", 4):
            '{"test":"thread_syscalls","workers":4,"seconds":1.0}'})
        _, errors = parse_perf_text(missing, SHA)
        self.assertTrue(any("must carry verified" in e for e in errors))
        nottrue = synth(raw_json={
            ("thread_syscalls", 4):
            '{"test":"thread_syscalls","workers":4,"seconds":1.0,'
            '"verified":false}'})
        _, errors = parse_perf_text(nottrue, SHA)
        self.assertTrue(any("verified must be true" in e for e in errors))

    def test_corebench_verified_forbidden(self):
        _, errors = parse_perf_text(synth(
            raw_json={("syscall", 4):
                      '{"test":"syscall","workers":1,"seconds":1.0,'
                      '"verified":true}'}), SHA)
        self.assertTrue(any("must not carry verified" in e for e in errors))

    def test_test_name_mismatch(self):
        _, errors = parse_perf_text(synth(
            raw_json={("syscall", 4):
                      '{"test":"signals","workers":1,"seconds":1.0}'}),
            SHA)
        self.assertTrue(any("!= workload" in e for e in errors))

    # ---- marker order ----------------------------------------------------
    def test_no_begin(self):
        _, errors = parse_perf_text(synth(begin=False), SHA)
        self.assertTrue(any("BEGIN count=0" in e for e in errors))
        self.assertTrue(any("misplaced" in e for e in errors))

    def test_duplicate_begin(self):
        _, errors = parse_perf_text(
            synth(duplicate_begin=True), SHA)
        self.assertTrue(any("duplicate or misplaced BEGIN" in e
                            for e in errors))

    def test_duplicate_end(self):
        _, errors = parse_perf_text(synth(extra_end=True), SHA)
        self.assertTrue(any("END count=2" in e for e in errors))

    def test_end_inside_run(self):
        # put END directly after a RUN marker, before its JSON/RC
        text = synth()
        text = text.replace(
            "HARDDIND_PERF_RUN binary=corebench workload=syscall args=1 "
            "warmup=0 seq=3\n",
            "HARDDIND_PERF_RUN binary=corebench workload=syscall args=1 "
            "warmup=0 seq=3\nHARDDIND_PERF_END status=0\n", 1)
        _, errors = parse_perf_text(text, SHA)
        self.assertTrue(any("END before protocol completion" in e
                            for e in errors))

    def test_extra_rc(self):
        _, errors = parse_perf_text(synth(extra_rc=True), SHA)
        self.assertTrue(any("RC marker outside a RUN" in e for e in errors))

    def test_missing_json_with_rc(self):
        _, errors = parse_perf_text(
            synth(no_json_rc={("syscall", 4)}), SHA)
        self.assertTrue(any("0 JSON records" in e for e in errors))

    def test_nonzero_end(self):
        _, errors = parse_perf_text(
            synth(end="HARDDIND_PERF_END status=1"), SHA)
        self.assertTrue(any("nonzero/malformed END" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
