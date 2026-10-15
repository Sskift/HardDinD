#!/usr/bin/env python3
"""Harness for faultaround_semantics_v2.

Strictly validates the test program's contract rather than trusting an
exit code:

  * every stdout line must be one JSON object (a deliberate failing
    fixture is run via --selfcheck and must produce parseable skip+fail
    records and exit code 3);
  * exactly one pass/fail/skip result per named case;
  * the final summary object must match the per-case tally.

Usage:
  run_semantics.py --binary ./faultaround_semantics_v2 [--label host|guest]
                   [--timeout-seconds 120]

Exit 0 when the harness ran and the guest binary reported no failures.
The label is recorded verbatim; only an explicit "--label guest" run may
be treated as kernel evidence - a host run validates the harness only.
"""
import argparse
import json
import pathlib
import subprocess
import sys


def run(binary, timeout, extra=()):
    p = subprocess.run([str(binary), *extra], capture_output=True,
                       timeout=timeout, text=True)
    rows, bad = [], []
    for i, line in enumerate(p.stdout.splitlines(), 1):
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict) or "case" not in obj or \
                    "result" not in obj:
                raise ValueError("missing case/result")
            rows.append(obj)
        except json.JSONDecodeError as e:
            bad.append({"line": i, "error": str(e), "raw": line})
    return p.returncode, rows, bad, p.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True, type=pathlib.Path)
    ap.add_argument("--label", default="host", choices=("host", "guest"))
    ap.add_argument("--timeout-seconds", type=int, default=120)
    a = ap.parse_args()

    out = {"test": "faultaround_semantics_v2", "label": a.label,
           "checks": []}
    nfail = 0

    # 1) deliberate failure/skip emission fixture
    try:
        rc, rows, bad, err = run(a.binary, 30, ("--selfcheck",))
    except subprocess.TimeoutExpired:
        print(json.dumps({"check": "selfcheck", "result": "fail",
                          "reason": "timeout"}))
        return 1
    by = {r["case"]: r["result"] for r in rows
          if r["case"] != "summary"}
    sc_ok = (rc == 3 and not bad and
             by.get("selfcheck_fail") == "fail" and
             by.get("selfcheck_skip") == "skip")
    out["checks"].append({"check": "selfcheck_emission",
                          "result": "pass" if sc_ok else "fail",
                          "rc": rc, "bad_lines": bad,
                          "stderr_tail": err[-500:]})
    if not sc_ok:
        nfail += 1

    # 2) full run
    try:
        rc, rows, bad, err = run(a.binary, a.timeout_seconds)
    except subprocess.TimeoutExpired:
        out["checks"].append({"check": "semantics", "result": "fail",
                              "reason": "timeout"})
        out["result"] = "fail"
        print(json.dumps(out, indent=1))
        return 1

    if bad:
        out["checks"].append({"check": "json_protocol", "result": "fail",
                              "bad_lines": bad})
        nfail += 1
    else:
        out["checks"].append({"check": "json_protocol", "result": "pass"})

    cases = [r for r in rows if r["case"] != "summary"]
    names = [r["case"] for r in cases]
    dupes = sorted({n for n in names if names.count(n) > 1})
    tally = {"pass": 0, "fail": 0, "skip": 0}
    for r in cases:
        if r["result"] in tally:
            tally[r["result"]] += 1
    summaries = [r for r in rows if r["case"] == "summary"]
    summary_ok = (len(summaries) == 1 and
                  all(summaries[0].get(k) == tally[k]
                      for k in ("pass", "fail", "skip")) and
                  summaries[0].get("result") ==
                      ("fail" if tally["fail"] else "pass"))
    if dupes or not summary_ok:
        out["checks"].append({"check": "per_case_integrity",
                              "result": "fail", "duplicates": dupes,
                              "summary": summaries, "tally": tally})
        nfail += 1
    else:
        out["checks"].append({"check": "per_case_integrity",
                              "result": "pass"})

    out["cases"] = cases
    out["tally"] = tally
    out["binary_rc"] = rc
    semantics_ok = rc == 0 and tally["fail"] == 0
    out["checks"].append({"check": "semantics",
                          "result": "pass" if semantics_ok else "fail"})
    if not semantics_ok:
        nfail += 1
    out["result"] = "pass" if nfail == 0 else "fail"
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
