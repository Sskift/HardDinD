#!/usr/bin/env python3
"""Record native CPU and actual GPU baselines, without calling Docker."""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument("work", type=pathlib.Path)
a = p.parse_args()
repo = pathlib.Path(__file__).resolve().parents[1]
work = a.work.resolve()
out = work / "results"
out.mkdir(parents=True, exist_ok=True)
binary = work / "native-corebench"
gpu = work / "native-cuda-probe"
subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-pthread",
                str(repo / "src/corebench.c"), "-o", str(binary)], check=True)
subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", str(repo / "src/cuda_probe.c"),
                "-ldl", "-o", str(gpu)], check=True)
with (out / "native-core.jsonl").open("w") as f:
    for test, workers in [("syscall", 1), ("exec", 1), ("threads", 1), ("threads", 4), ("processes", 1), ("processes", 4)]:
        for _ in range(3):
            text = subprocess.check_output([str(binary), test, str(workers)], text=True, timeout=60)
            json.loads(text)
            f.write(text)
with (out / "native-cuda.jsonl").open("w") as f, (out / "native-cuda.stderr").open("w") as err:
    result = subprocess.run([str(gpu)], stdout=f, stderr=err, timeout=120)
paths = [binary, work / "probe-root/corebench"]
hashes = []
for path in paths:
    hashes.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path))
(out / "benchmark-sha256.txt").write_text("\n".join(hashes) + "\n")
sys.exit(result.returncode)
