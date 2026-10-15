#!/usr/bin/env python3
"""Reject relocated UML stub code that references unmapped kernel storage."""
import argparse
import json
import pathlib
import re
import subprocess

p = argparse.ArgumentParser()
p.add_argument("object", type=pathlib.Path)
a = p.parse_args()
section = ".__syscall_stub"
symbols = subprocess.check_output(["objdump", "-t", str(a.object)], text=True)
local = {line.split()[-1] for line in symbols.splitlines() if section in line.split()}
if not {"stub_syscall_handler", "stub_signal_interrupt"} <= local:
    raise SystemExit("missing stub code symbols")
relocations = subprocess.check_output(["objdump", "-r", "-j", section, str(a.object)], text=True)
targets = []
for line in relocations.splitlines():
    if re.match(r"^[0-9a-fA-F]+\s+R_", line):
        targets.append(re.split(r"[+-]0x", line.split()[2])[0])
outside = sorted(set(targets) - local)
report = {"object": str(a.object), "section": section, "relocation_targets": targets,
          "external_targets": outside, "passed": not outside}
print(json.dumps(report, indent=2))
raise SystemExit(1 if outside else 0)
