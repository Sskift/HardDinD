#!/usr/bin/env python3
"""Apply an overlapping patch series reproducibly without replacing local edits."""
import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def content(path):
    return path.read_bytes() if path.exists() else None


def apply_series(source, archive, patches):
    stamp = source / ".harddind-patches.json"
    previous = json.loads(stamp.read_text()) if stamp.exists() else {}
    paths = set(previous.get("files", {}))
    for patch in patches:
        for line in patch.read_text().splitlines():
            if line.startswith(("--- a/", "+++ b/")):
                paths.add(line[6:].split("\t", 1)[0])
    for name in paths:
        p = pathlib.PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not name.startswith(("arch/um/", "fs/fuse/")):
            raise ValueError(f"unexpected patch path: {name}")
        if not (source / p).resolve().is_relative_to(source.resolve()):
            raise ValueError(f"patch path escapes source: {name}")
    with tempfile.TemporaryDirectory(prefix="harddind-patches-") as directory:
        stage = pathlib.Path(directory)
        # Stream xz once; random extraction repeatedly decompresses the kernel.
        with tarfile.open(archive, mode="r|*") as tar:
            prefix = source.name + "/"
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
        # Recognize pristine files and any already applied prefix of this series.
        known = {name: {digest(content(stage / name))} for name in paths}
        for patch in patches:
            subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-d", str(stage)],
                           input=patch.read_bytes(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for name in paths:
                known[name].add(digest(content(stage / name)))
        for name in paths:
            if name in previous.get("files", {}):
                known[name].add(previous["files"][name])
            if digest(content(source / name)) not in known[name]:
                raise ValueError(f"refusing to overwrite local kernel edit: {name}")
        # All validation happens before the first source write.
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


if __name__ == "__main__":
    repo = pathlib.Path(__file__).resolve().parents[1]
    src = pathlib.Path(sys.argv[1]).resolve()
    patches = sorted((repo / "kernel/patches").glob("[0-9]*.patch"))
    if sys.argv[3:] == ["--handoff-experiment"]:
        patches += [repo / "kernel/experiments/0002-um-wake-only-sleeping-peer.patch"]
    elif sys.argv[3:] == ["--threads-experiment"]:
        patches += [repo / "kernel/experiments/0003-um-shared-vm-runners.patch"]
    elif sys.argv[3:] == ["--dispatch-experiment"]:
        patches += [repo / "kernel/experiments/0003-um-shared-vm-runners.patch",
                    repo / "kernel/experiments/0004-um-parallel-waiter-handoff.patch"]
    elif sys.argv[3:] in (["--cache-experiment"], ["--sync-experiment"], ["--performance-experiment"], ["--ipi-experiment"], ["--protect-experiment"], ["--gpu-experiment"]):
        patches += [repo / "kernel/experiments/0003-um-shared-vm-runners.patch",
                    repo / "kernel/experiments/0004-um-parallel-waiter-handoff.patch",
                    repo / "kernel/experiments/0005-um-cache-clean-mapping-brokers.patch"]
        if sys.argv[3:] != ["--cache-experiment"]:
            patches += [repo / "kernel/experiments/0006-um-skip-idle-mapping-sync.patch"]
        if sys.argv[3:] in (["--performance-experiment"], ["--ipi-experiment"], ["--protect-experiment"], ["--gpu-experiment"]):
            patches += [repo / "kernel/experiments/0007-um-retain-broker-memory-fd.patch"]
        if sys.argv[3:] in (["--ipi-experiment"], ["--protect-experiment"], ["--gpu-experiment"]):
            patches += [repo / "kernel/experiments/0008-um-defer-ipi-masking.patch",
                        repo / "kernel/experiments/0009-um-synchronize-deferred-ipi-idle.patch"]
        if sys.argv[3:] == ["--protect-experiment"]:
            patches += [repo / "kernel/experiments/0010-um-update-mapping-protection.patch"]
        if sys.argv[3:] == ["--gpu-experiment"]:
            patches += [repo / "kernel/experiments/0012-um-gpu-control-transport.patch",
                        repo / "kernel/experiments/0013-um-gpu-register-fd.patch",
                        repo / "kernel/experiments/0016-um-gpu-rm-client.patch",
                        repo / "kernel/experiments/0019-um-gpu-root-control.patch",
                        repo / "kernel/experiments/0020-um-gpu-control-pointer-errors.patch",
                        repo / "kernel/experiments/0021-um-gpu-device-numbers.patch",
                        repo / "kernel/experiments/0022-um-gpu-build-version.patch"]
    elif sys.argv[3:]:
        raise SystemExit("unknown patch-series option")
    apply_series(src, pathlib.Path(sys.argv[2]), patches)
