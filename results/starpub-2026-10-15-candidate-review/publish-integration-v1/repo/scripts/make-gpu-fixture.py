#!/usr/bin/env python3
"""Build a Linux/glibc GPU test image with the outer NVIDIA userspace driver.

This packages libraries only. It does NOT grant devices or implement a guest
GPU driver. Container GPU execution is still required to pass acceptance.
"""
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

work = pathlib.Path(sys.argv[1]).resolve()
binary = work / "docker-root/cuda-probe"
listing = subprocess.check_output(["ldconfig", "-p"], text=True)
wanted = ["libcuda.so.1", "libnvidia-ptxjitcompiler.so.1"]
libraries = []
for name in wanted:
    matches = [line.split("=>", 1)[1].strip() for line in listing.splitlines()
               if line.strip().startswith(name + " ") and "x86-64" in line and "=>" in line]
    if not matches:
        raise SystemExit("missing outer GPU library: " + name)
    libraries.append(pathlib.Path(matches[0]))

all_files = set(libraries)
for file in [binary] + libraries:
    output = subprocess.check_output(["ldd", str(file)], text=True)
    if "not found" in output:
        raise SystemExit("unresolved shared library: " + output)
    for path in re.findall(r"(/[^\s()]+)", output):
        all_files.add(pathlib.Path(path))

manifest = {}
with tempfile.TemporaryDirectory(prefix="gpu-fixture-", dir=work) as temp:
    root = pathlib.Path(temp)
    for directory in ["dev", "proc", "sys", "tmp", "etc", "run"]:
        (root / directory).mkdir()
    shutil.copy2(binary, root / "cuda-probe")
    for file in sorted(all_files):
        target = root / file.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target, follow_symlinks=True)
        manifest[str(file)] = hashlib.sha256(target.read_bytes()).hexdigest()
    with tarfile.open(work / "docker-root/gpu-rootfs.tar", "w") as tar:
        for path in sorted(root.iterdir()):
            tar.add(path, arcname=path.name)
manifest["/cuda-probe"] = hashlib.sha256(binary.read_bytes()).hexdigest()
(work / "results/gpu-fixture.json").write_text(json.dumps(manifest, indent=2) + "\n")
