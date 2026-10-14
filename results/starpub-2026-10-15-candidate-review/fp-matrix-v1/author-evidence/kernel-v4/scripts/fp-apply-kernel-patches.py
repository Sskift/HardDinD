#!/usr/bin/env python3
"""Apply the CURRENT owner series (0003 parallel + 0017/0023/0025 GPU stack)
with the round6 0018 lazy-XSTATE experiment stacked last.

Fork of apply-kernel-patches.py with two deliberate differences:
  * the path allowlist additionally covers arch/x86/um/ (0018 lives there);
  * the series is the complete current experiment set, not a single knob.

Usage: fp-apply-kernel-patches.py <source-dir> <pristine-archive> [--lazy]
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

# Base patches always present in the owner runtime kernel.
BASE = ["0001-um-bounded-seccomp-polling.patch",
        "0011-fuse-virtio-interrupts.patch",
        "0014-um-check-usercopy-read-permission.patch",
        "0015-um-opt-in-memfd-backing.patch"]

# The owner GPU kernel chain (GPU_EXPERIMENT implies IPI->PERF->CACHE->
# DISPATCH->THREADS; gpu-async adds 0017; 0025 closes the chain). 0002
# (legacy handoff) and 0010 (protect) are mutually exclusive alternatives
# and are not part of that kernel. 0018 is deliberately applied last.
EXPERIMENTS = [
    "0003-um-shared-vm-runners.patch",
    "0004-um-parallel-waiter-handoff.patch",
    "0005-um-cache-clean-mapping-brokers.patch",
    "0006-um-skip-idle-mapping-sync.patch",
    "0007-um-retain-broker-memory-fd.patch",
    "0008-um-defer-ipi-masking.patch",
    "0009-um-synchronize-deferred-ipi-idle.patch",
    "0012-um-gpu-control-transport.patch",
    "0013-um-gpu-register-fd.patch",
    "0016-um-gpu-rm-client.patch",
    "0019-um-gpu-root-control.patch",
    "0020-um-gpu-control-pointer-errors.patch",
    "0021-um-gpu-device-numbers.patch",
    "0022-um-gpu-build-version.patch",
    "0023-um-gpu-client-policy.patch",
    # Owner applier inserts the async-wait experiment after 0023, before 0025.
    "0017-um-gpu-async-wait.patch",
    "0025-um-gpu-policy-driver-validation.patch",
    # Current GPU baseline (main 0210540): both touch only arch/um/kernel/gpu.c.
    "0026-um-gpu-fixed-alloc.patch",
    "0027-um-gpu-system-features.patch",
]
LAZY_PATCH = "0018-um-lazy-xstate.patch"

ALLOWED_PREFIXES = ("arch/um/", "arch/x86/um/", "fs/fuse/")


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def content(path):
    return path.read_bytes() if path.exists() else None


def series_paths(patches):
    paths = set()
    for patch in patches:
        for line in patch.read_text().splitlines():
            if line.startswith(("--- a/", "+++ b/")):
                paths.add(line[6:].split("\t", 1)[0])
    return paths


def apply_series(source, archive, patches):
    """Same validation/atomic-swap semantics as apply-kernel-patches.py."""
    stamp = source / ".harddind-patches.json"
    previous = json.loads(stamp.read_text()) if stamp.exists() else {}
    paths = set(previous.get("files", {})) | series_paths(patches)
    for name in paths:
        p = pathlib.PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not name.startswith(ALLOWED_PREFIXES):
            raise ValueError(f"unexpected patch path: {name}")
        if not (source / p).resolve().is_relative_to(source.resolve()):
            raise ValueError(f"patch path escapes source: {name}")
    with tempfile.TemporaryDirectory(prefix="harddind-fp-patches-") as directory:
        stage = pathlib.Path(directory)
        with tarfile.open(archive, mode="r|*") as tar:
            prefix = ""
            for member in tar:  # streaming: first member is the bare directory
                if "/" in member.name:
                    prefix = member.name.split("/", 1)[0] + "/"
                    break
            if not prefix:
                raise ValueError("archive has no top-level directory")
        with tarfile.open(archive, mode="r|*") as tar:
            for member in tar:
                if not member.name.startswith(prefix):
                    continue
                name = member.name[len(prefix):]
                if name not in paths:
                    continue
                if not member.isfile():
                    raise ValueError(f"patch target is not a regular file: {name}")
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
        known = {name: {digest(content(stage / name))} for name in paths}
        for patch in patches:
            subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1",
                            "-d", str(stage)],
                           input=patch.read_bytes(), check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for name in paths:
                known[name].add(digest(content(stage / name)))
        for name in paths:
            if name in previous.get("files", {}):
                known[name].add(previous["files"][name])
            if digest(content(source / name)) not in known[name]:
                raise ValueError(f"refusing to overwrite local kernel edit: {name}")
        for name in paths:
            value = content(stage / name)
            target = source / name
            if value == content(target):
                continue
            if value is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".harddind-new")
                temporary.write_bytes(value)
                temporary.replace(target)
        stamp.write_text(json.dumps({
            "patches": {p.name: digest(p.read_bytes()) for p in patches},
            "files": {name: digest(content(stage / name)) for name in sorted(paths)},
        }, indent=2) + "\n")


def collect_patches(lazy):
    patches = [REPO / "kernel/patches" / name for name in BASE]
    patches += [REPO / "kernel/experiments" / name for name in EXPERIMENTS]
    if lazy:
        patches.append(REPO / "kernel/experiments" / LAZY_PATCH)
    for patch in patches:
        if not patch.exists():
            raise SystemExit(f"missing pinned patch: {patch}")
    return patches


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[3] not in ("--eager", "--lazy"):
        raise SystemExit(__doc__)
    src = pathlib.Path(sys.argv[1]).resolve()
    archive = pathlib.Path(sys.argv[2]).resolve()
    apply_series(src, archive, collect_patches(sys.argv[3] == "--lazy"))
    print("applied: " + " ".join(p.name for p in collect_patches(sys.argv[3] == "--lazy")))
