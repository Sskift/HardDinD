#!/usr/bin/env python3
"""Fail closed: diagnostics and partial suites cannot certify the product."""
import argparse
import json
import pathlib
import sys

REQUIRED = ("docker_functionality", "native_performance", "gpu_in_container")

def failures(report):
    errors = []
    if report.get("scope") != "complete_docker_native_performance_gpu":
        errors.append("acceptance scope missing or changed")
    checks = report.get("requirements", {})
    for name in REQUIRED:
        check = checks.get(name, {})
        if check.get("status") != "pass":
            errors.append(f"{name}: {check.get('status', 'missing')}")
        if not check.get("evidence"):
            errors.append(f"{name}: no evidence")
    # The baseline must be comparable; a fast benchmark on different hardware
    # is not evidence for the user's performance requirement.
    if checks.get("native_performance", {}).get("same_host_and_resources") is not True:
        errors.append("native_performance: comparable baseline missing")
    return errors

def main():
    p = argparse.ArgumentParser()
    p.add_argument("report", type=pathlib.Path)
    a = p.parse_args()
    try:
        report = json.loads(a.report.read_text())
        errors = failures(report)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print(f"NOT ACCEPTED: invalid report: {e}")
        return 1
    if errors:
        print("NOT ACCEPTED")
        print("\n".join("- " + e for e in errors))
        return 1
    print("REPORTED REQUIREMENTS PASS; review referenced evidence before release")
    return 0

if __name__ == "__main__":
    sys.exit(main())
