"""Parser/validator for the HARDDIND_PERF console protocol (v2).

Same marker protocol is emitted by the guest init (harddind-perf-init) and
synthesized by the supervisor for native brackets. A cell is valid only
when ALL of the following hold:

  * exactly one BEGIN, appearing before every other protocol record;
  * both binary SHA-256 markers appear once, after BEGIN and before the
    first RUN, and match the built binaries;
  * exactly len(WORKLOADS)*7 RUN markers in the fixed order with seq 1..7
    and warmup=1 on seq 1-2 / 0 on seq 3-7; a RUN is always closed by its
    own RC before the next RUN (duplicate/misplaced RC is rejected);
  * every RUN encloses exactly one benchmark JSON line:
      {"test": <workload>, "workers": <expected int>, "seconds": <finite
       number strictly > 0>}  (bool is not accepted as int/float)
    corebench JSON must carry no "verified" key; dispatchbench JSON must
    carry "verified": true (exactly what the frozen sources emit);
  * JSON appearing outside a RUN is rejected;
  * exactly one END status=0, only after the final RC, with nothing after.

Returns (runs, errors). Timeout/nonzero rc surfaces as an error record.
"""
import json
import math
import re

RUN_RE = re.compile(
    r"^HARDDIND_PERF_RUN "
    r"binary=(\S+) workload=(\S+) args=(\S+) warmup=([01]) seq=(\d+)$")
RC_RE = re.compile(r"^HARDDIND_PERF_RUN_RC=(-?\d+)$")
SHA_RE = re.compile(
    r"^HARDDIND_PERF_(COREBENCH|DISPATCHBENCH)_SHA256=([0-9a-f]{64})$")

# (binary, workload, args, expected workers). args "-" == no extra argv.
WORKLOADS = [
    ("corebench", "syscall", "1", 1),
    ("corebench", "signals", "1", 1),
    ("corebench", "threads", "4", 4),
    ("corebench", "processes", "4", 4),
    ("dispatchbench", "thread_syscalls", "-", 4),
]
ITERS = 7
WARMUPS = 2
TOTAL_RUNS = len(WORKLOADS) * ITERS
MARK_BEGIN = "HARDDIND_PERF_BEGIN"
MARK_END = "HARDDIND_PERF_END status=0"

# Phases: pre-begin -> begin -> (sha) -> run blocks -> post-runs -> end.
PHASE_PRE = "pre"
PHASE_OPEN = "open"        # after BEGIN, before first RUN
PHASE_IN_RUN = "in_run"
PHASE_POST_RUNS = "post"
PHASE_END = "end"


def _is_plain_int(v):
    return type(v) is int


def _is_plain_number(v):
    return type(v) in (int, float) and not isinstance(v, bool)


def parse_perf_text(text, expect_sha):
    """expect_sha: {'corebench': sha, 'dispatchbench': sha}."""
    runs = []
    errors = []
    begin = end = 0
    seen_sha = {}
    phase = PHASE_PRE
    cur = None
    seen_json = 0
    run_count = 0

    def close_run():
        nonlocal cur, seen_json
        if cur is None:
            return
        if seen_json != 1:
            errors.append("run %s/%s seq=%s has %d JSON records (need 1)"
                          % (cur["binary"], cur["workload"], cur["seq"],
                             seen_json))
        if cur.get("rc") is None:
            errors.append("run %s/%s seq=%s missing RC marker"
                          % (cur["binary"], cur["workload"], cur["seq"]))
        elif cur["rc"] != 0:
            errors.append("run %s/%s seq=%s rc=%s"
                          % (cur["binary"], cur["workload"], cur["seq"],
                             cur["rc"]))
        runs.append(cur)
        cur = None
        seen_json = 0

    def reject(line_no, msg):
        errors.append("line %d: %s: %r" % (line_no + 1, msg,
                      None))

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line == MARK_BEGIN:
            if phase != PHASE_PRE:
                errors.append("line %d: duplicate or misplaced BEGIN"
                              % (i + 1))
            begin += 1
            phase = PHASE_OPEN
            continue
        if line.startswith("HARDDIND_PERF_END "):
            if line != MARK_END:
                errors.append("line %d: nonzero/malformed END: %s"
                              % (i + 1, line))
            if phase in (PHASE_PRE, PHASE_IN_RUN):
                errors.append("line %d: END before protocol completion "
                              "(phase=%s)" % (i + 1, phase))
            end += 1
            phase = PHASE_END
            continue
        m = SHA_RE.match(line)
        if m:
            key = m.group(1).lower()
            if phase != PHASE_OPEN:
                errors.append("line %d: SHA marker %s misplaced"
                              % (i + 1, key))
            if key in seen_sha:
                errors.append("line %d: duplicate SHA marker %s"
                              % (i + 1, key))
            seen_sha[key] = m.group(2)
            continue
        m = RUN_RE.match(line)
        if m:
            if phase in (PHASE_PRE, PHASE_POST_RUNS, PHASE_END):
                errors.append("line %d: RUN marker misplaced (phase=%s)"
                              % (i + 1, phase))
            if phase == PHASE_IN_RUN:
                errors.append("line %d: nested RUN marker without RC"
                              % (i + 1))
            close_run()  # closes only if somehow open (also reported above)
            cur = {"binary": m.group(1), "workload": m.group(2),
                   "args": m.group(3), "warmup": int(m.group(4)),
                   "seq": int(m.group(5)), "rc": None, "json": None,
                   "line": i}
            run_count += 1
            phase = PHASE_IN_RUN
            continue
        m = RC_RE.match(line)
        if m:
            if phase != PHASE_IN_RUN or cur is None:
                errors.append("line %d: RC marker outside a RUN: %s"
                              % (i + 1, line))
                continue
            cur["rc"] = int(m.group(1))
            close_run()
            phase = PHASE_POST_RUNS if run_count == TOTAL_RUNS else PHASE_OPEN
            continue
        if line.startswith("{"):
            if phase != PHASE_IN_RUN or cur is None:
                errors.append("line %d: JSON record outside a RUN block"
                              % (i + 1))
                continue
            seen_json += 1
            try:
                obj = json.loads(line)
            except ValueError:
                errors.append("line %d: malformed JSON" % (i + 1))
                continue
            err = _validate_json(obj, cur)
            if err:
                errors.append("line %d: %s" % (i + 1, err))
            else:
                cur["json"] = obj

    close_run()

    if begin != 1:
        errors.append("BEGIN count=%d (need exactly 1)" % begin)
    if end != 1:
        errors.append("END count=%d (need exactly 1)" % end)
    for key in ("corebench", "dispatchbench"):
        want = expect_sha.get(key)
        got = seen_sha.get(key)
        if got is None:
            errors.append("missing SHA marker " + key)
        elif want is not None and got != want:
            errors.append("%s hash mismatch: marker=%s expected=%s"
                          % (key, got, want))

    want_seq = [(b, w, a, n, it + 1)
                for (b, w, a, n) in WORKLOADS
                for it in range(ITERS)]
    got_seq = [(r["binary"], r["workload"], r["args"], None, r["seq"])
               for r in runs]
    if [(x[0], x[1], x[2], x[4]) for x in got_seq] != \
            [(x[0], x[1], x[2], x[4]) for x in want_seq] or \
            len(runs) != TOTAL_RUNS:
        errors.append("run sequence/count mismatch: got %d runs, expected %d"
                      % (len(runs), TOTAL_RUNS))
    for r in runs:
        expect_warm = 1 if r["seq"] <= WARMUPS else 0
        if r["warmup"] != expect_warm:
            errors.append("warmup tag wrong for %s/%s seq=%s: %d"
                          % (r["binary"], r["workload"], r["seq"],
                             r["warmup"]))
    return runs, sorted(set(errors))


def _validate_json(obj, cur):
    if not isinstance(obj, dict):
        return "JSON record is not an object"
    if obj.get("test") != cur["workload"]:
        return "JSON test=%r != workload=%r" % (obj.get("test"),
                                                cur["workload"])
    workers = obj.get("workers")
    if not _is_plain_int(workers) or workers <= 0:
        return "workers must be a positive integer, got %r" % (workers,)
    seconds = obj.get("seconds")
    if not _is_plain_number(seconds):
        return "seconds must be a number, got %r" % (seconds,)
    if not math.isfinite(seconds) or seconds <= 0:
        return "seconds must be finite and strictly positive, got %r" \
               % (seconds,)
    if cur["binary"] == "dispatchbench":
        if "verified" not in obj:
            return "dispatchbench JSON must carry verified (frozen source)"
        if obj["verified"] is not True:
            return "dispatchbench verified must be true, got %r" \
                   % (obj["verified"],)
    elif "verified" in obj:
        return "corebench JSON must not carry verified (frozen source schema)"
    return _validate_workers(workers, cur)


def _validate_workers(workers, cur):
    want = None
    for (b, w, _a, n) in WORKLOADS:
        if b == cur["binary"] and w == cur["workload"]:
            want = n
    if want is None:
        return "unknown binary/workload %s/%s" % (cur["binary"],
                                                  cur["workload"])
    if workers != want:
        return "workers=%d but %s/%s requires %d" % (workers, cur["binary"],
                                                     cur["workload"], want)
    return None


def timed_records(runs):
    """Timed (non-warmup) records with valid JSON and rc 0."""
    return [r for r in runs
            if r["warmup"] == 0 and r["rc"] == 0 and r["json"] is not None]
